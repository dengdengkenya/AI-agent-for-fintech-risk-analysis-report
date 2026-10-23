"""Check local Ollama connectivity and generate a response with Qwen3 4B.

Run: python test_ollama.py
Uses only the Python standard library. Exit code 0 means success; 1 means failure.
"""

import argparse
import json
import sys
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def request_json(base_url, path, *, timeout, payload=None):
    """Send a GET or JSON POST, bypassing proxies for the local server."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path}: HTTP {error.code}: {detail}") from error
    except TimeoutError as error:
        raise RuntimeError(
            f"{path} timed out after {timeout:g} seconds. "
            "The first model load can be slow; try --timeout 300."
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Cannot connect to {base_url}: {error.reason}. "
            "Open the Ollama app (or run 'ollama serve') and check --host."
        ) from error
    if not isinstance(result, dict):
        raise RuntimeError(f"{path}: expected a JSON object from Ollama.")
    if result.get("error"):
        raise RuntimeError(f"{path}: {result['error']}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="http://localhost:11434", help="Ollama server URL."
    )
    parser.add_argument("--model", default="qwen3:4b", help="Installed model name.")
    parser.add_argument(
        "--timeout", type=int, default=180,
        help="Generation timeout in seconds (default: 180).",
    )
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be at least 1 second")
    base_url = args.host.rstrip("/")
    url = urlsplit(base_url)
    if url.scheme not in ("http", "https") or not url.hostname:
        parser.error("--host must be an http:// or https:// URL")

    try:
        print(f"Checking Ollama at {base_url} ...", flush=True)
        check_timeout = min(args.timeout, 10)
        version = request_json(base_url, "/api/version", timeout=check_timeout)
        print(f"[PASS] Server is reachable (Ollama {version.get('version', 'unknown')}).")

        installed = request_json(base_url, "/api/tags", timeout=check_timeout)
        names = [item["name"] for item in installed.get("models", [])]
        if args.model not in names:
            raise RuntimeError(
                f"Model '{args.model}' is not installed. "
                f"Available models: {', '.join(names) or '(none)'}. "
                f"Install it with: ollama pull {args.model}"
            )
        print(f"[PASS] Model is installed: {args.model}")

        prompt = "What is 2 + 2? Reply with only the number. /no_think"
        print(f"Sending prompt: {prompt}", flush=True)
        started = perf_counter()
        result = request_json(
            base_url, "/api/generate", timeout=args.timeout,
            payload={
                "model": args.model,
                "prompt": prompt,
                "stream": False,
                # Parse reasoning separately if the installed template emits it.
                # The /no_think prompt switch requests a short, direct answer.
                "think": True,
                "keep_alive": "5m",
                "options": {"temperature": 0, "num_predict": 128, "num_ctx": 2048},
            },
        )
        elapsed = perf_counter() - started
        reply = result.get("response", "")
        if result.get("done") is not True or not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("The model did not return a completed, nonempty response.")
        if result.get("done_reason") == "length":
            raise RuntimeError("The model hit the output limit before completing its reply.")
        print(f"[PASS] Model generated a response in {elapsed:.2f} seconds.")
        print(f"Response: {reply.strip()}")

        running = request_json(base_url, "/api/ps", timeout=check_timeout)
        if not any(item.get("name") == args.model for item in running.get("models", [])):
            raise RuntimeError("Generation succeeded, but the model is no longer loaded.")
        print(f"[PASS] {args.model} is loaded and running.")
        print("SUCCESS: Local Ollama model access and generation both work.")
        return 0
    except (RuntimeError, OSError, ValueError) as error:
        print(f"[FAIL] {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
