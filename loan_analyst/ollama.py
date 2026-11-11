"""A small client for a local Ollama server.

Only a loopback HTTP endpoint is accepted. Proxies are disabled and redirects
are refused, so loan aggregates cannot leave the machine. Any object with the
same ``preflight()`` and ``chat()`` methods can replace OllamaClient in tests.

API references:
https://docs.ollama.com/api/chat
https://docs.ollama.com/capabilities/tool-calling
"""

from contextlib import suppress
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .settings import ModelConfig, local_url
from .support import read_json

MAX_RESPONSE_BYTES = 2_000_000
MAX_REQUEST_BYTES = 400_000


class ModelError(RuntimeError):
    """An actionable failure that is safe to show without raw service response contents.

    ``transient`` marks failures worth one identical retry: a timeout, a refused
    connection or a server-side (5xx) error. Everything else needs a human.
    """

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


def _context_length(details):
    """The model's trained context window from /api/show, when the server reports it."""
    info = details.get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if str(key).endswith(".context_length") and type(value) is int and value > 0:
            return value
    return None


class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects instead of following them to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError("Ollama redirected the request; use a direct local endpoint")


def _requires_thinking(details):
    """Recognize thinking-only models even when an alias hides their variant.

    New servers advertise thinking controls. Older Ollama versions expose the
    GGUF identity instead; Qwen3 Thinking releases cannot honor think=False.
    Hybrid Qwen3 models remain supported.
    """
    thinking = details.get("thinking")
    values = thinking.get("values") if isinstance(thinking, dict) else None
    if isinstance(values, list) and values:
        return not any(value is False for value in values)

    info = details.get("model_info")
    if not isinstance(info, dict):
        return False
    name = str(info.get("general.name", "")).lower()
    family = str(info.get("general.architecture", "")).lower()
    finetune = str(info.get("general.finetune", "")).lower()
    return (family == "qwen3" or "qwen3" in name) and ("thinking" in name or "thinking" in finetune)


def request_json(base_url, path, *, payload=None, timeout=10):
    """Send one JSON request to the local server and return its JSON object."""
    base_url = local_url(base_url)
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
    request = Request(base_url + path, data=data, headers={"Content-Type": "application/json"})

    # Keep local traffic local even on machines with HTTP_PROXY configured.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelError("Ollama response exceeds the size limit")

        result = read_json(raw)
        if not isinstance(result, dict) or "error" in result:
            raise ModelError("Ollama returned an invalid or unsuccessful response")
        return result
    except HTTPError as error:
        with suppress(Exception):
            error.close()  # discard the body unread: service error text is never relayed
        raise ModelError(f"Ollama HTTP {error.code}; check the local model and server",
                         transient=error.code >= 500) from None
    except (URLError, TimeoutError, OSError):
        raise ModelError("Local Ollama is unavailable or timed out; start Ollama or increase "
                         "--ollama-timeout", transient=True) from None
    except (ValueError, UnicodeError):
        raise ModelError("Ollama returned invalid JSON") from None


class OllamaClient:
    """Minimal local Ollama client used by the analyst."""

    def __init__(self, config):
        self.config = config
        self.metadata = None

    def preflight(self, *, require_tools=True):
        """Check the installed local model before any expensive database scan.

        This never pulls a model. It rejects missing models, cloud models and
        thinking-only variants that cannot honor disabled thinking.
        """
        config = self.config
        models = request_json(config.url, "/api/tags").get("models")
        if not isinstance(models, list):
            raise ModelError("Ollama returned an invalid model inventory")

        model = next((item for item in models if isinstance(item, dict)
                      and config.model in (item.get("name"), item.get("model"))), None)
        if model is None:
            raise ModelError(f"{config.model} is not installed locally; run `ollama pull {config.model}` first")

        if (model.get("remote_model") or model.get("remote_host")
                or "cloud" in config.model.lower() or not model.get("size")):
            raise ModelError("Analysis requires a locally installed model, not a cloud model")

        details = request_json(config.url, "/api/show", payload={"model": config.model})
        capabilities = details.get("capabilities")
        if require_tools and isinstance(capabilities, list) and "tools" not in capabilities:
            raise ModelError(f"{config.model} does not support tool calling; choose a tools-capable local "
                             f"model such as {ModelConfig().model}")

        if _requires_thinking(details):
            recommended = ModelConfig().model
            raise ModelError(f"{config.model} resolves to a thinking-only model and cannot disable thinking. "
                             f"Run `ollama pull {recommended}`, then use --ollama-model {recommended}. "
                             "Reuse an existing snapshot with --snapshot latest; MySQL was not queried")

        context_length = _context_length(details)
        if context_length is not None and config.num_ctx > context_length:
            raise ModelError(f"--num-ctx {config.num_ctx} exceeds the {context_length}-token context window "
                             f"of {config.model}; use --num-ctx {context_length} or less")

        version = request_json(config.url, "/api/version")
        self.metadata = {
            "context_length": context_length,
            "model": config.model,
            "model_digest": model.get("digest"),
            "server_version": version.get("version"),
            "endpoint": config.url,
            "capabilities": capabilities if isinstance(capabilities, list) else None,
        }
        return self.metadata

    def chat(self, messages, *, tools=None, format=None, num_predict=1024):
        """One non-streaming chat turn. Thinking is disabled and never stored."""
        payload = {
            "model": self.config.model,
            # Snapshot the messages so a later change cannot alter the saved request.
            "messages": json.loads(json.dumps(messages)),
            "stream": False,
            "think": False,
            "keep_alive": self.config.keep_alive,
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": self.config.num_ctx,
                "num_predict": num_predict,
            },
        }
        if tools:
            payload["tools"] = tools
        if format is not None:
            payload["format"] = format

        if len(json.dumps(payload, ensure_ascii=False)) > MAX_REQUEST_BYTES:
            raise ModelError("Analysis context is too large; use fewer cutoffs or tool rounds")

        response = request_json(self.config.url, "/api/chat", payload=payload,
                                timeout=self.config.timeout)

        if response.get("done_reason") == "length":
            raise ModelError(f"Ollama reached the response token limit (num_predict={num_predict}); "
                             "the partial response was discarded and no output was replaced. "
                             "Use a model that supports disabled thinking and fewer tool rounds; "
                             "reuse the saved data with --snapshot latest")
        if response.get("done") is not True or response.get("done_reason") not in ("stop", "tool_calls"):
            raise ModelError("Model response was incomplete; no output was replaced")

        message = response.get("message")
        content = message.get("content", "") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ModelError("Model returned no message content")

        calls = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") if isinstance(item, dict) else None
            function = function if isinstance(function, dict) else {}
            calls.append({"name": function.get("name"), "arguments": function.get("arguments")})

        return {
            "request": payload,
            "content": content,
            "tool_calls": calls,
            "usage": {key: response.get(key) for key in (
                "prompt_eval_count", "eval_count", "total_duration", "load_duration")},
        }
