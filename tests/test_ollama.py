"""Local Ollama client boundary. Never contacts a running Ollama server."""

import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from loan_analyst import ollama as llm
from loan_analyst.settings import ModelConfig


def response(content="{}", tool_calls=None, done_reason="stop"):
    message = {"role": "assistant", "content": content, "thinking": "do not persist internal reasoning"}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"done": True, "done_reason": done_reason, "message": message, "prompt_eval_count": 80, "eval_count": 30}


class OllamaClientTests(unittest.TestCase):
    def setUp(self):
        self.client = llm.OllamaClient(ModelConfig())

    def test_requests_disable_proxies_and_redirects_and_bound_responses(self):
        reply = Mock()
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        reply.read.return_value = b'{"done": true}'
        opener = Mock()
        opener.open.return_value = reply
        with patch.object(llm, "build_opener", return_value=opener) as build:
            self.assertEqual(llm.request_json("http://localhost:11434", "/api/chat", payload={"model": "m"},
                                              timeout=17), {"done": True})
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], llm._NoRedirect)
        with self.assertRaises(llm.ModelError):
            handlers[1].redirect_request(None, None, 302, "", {}, "https://example.com")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(json.loads(request.data), {"model": "m"})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 17)
        reply.read.assert_called_once_with(llm.MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(ValueError):
            llm.request_json("https://ollama.com", "/api/chat")

    def test_service_errors_are_actionable_and_never_echo_bodies(self):
        for error in (TimeoutError("private value"), URLError("private value"),
                      HTTPError("http://localhost", 500, "private value", {}, io.BytesIO(b"private value"))):
            with patch.object(llm, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(llm.ModelError) as raised:
                    llm.request_json("http://localhost:11434", "/api/chat")
            self.assertNotIn("private value", str(raised.exception))

    def test_preflight_requires_an_installed_local_tool_capable_model(self):
        installed = {"models": [{"name": self.client.config.model, "size": 100, "digest": "d"}]}
        tools = {"capabilities": ["completion", "tools"]}
        with patch.object(llm, "request_json", side_effect=[installed, tools, {"version": "0.12"}]):
            metadata = self.client.preflight()
        self.assertEqual((metadata["model_digest"], metadata["server_version"]), ("d", "0.12"))
        with patch.object(llm, "request_json", side_effect=[installed, {}, {"version": "old"}]):
            self.assertIsNone(self.client.preflight()["capabilities"])
        failures = [[{"models": []}], [{"models": None}],
                    [{"models": [{"name": self.client.config.model, "size": 100, "remote_host": "cloud"}]}],
                    [installed, {"capabilities": ["completion"]}]]
        for replies in failures:
            with self.subTest(replies=replies), patch.object(llm, "request_json", side_effect=replies), \
                 self.assertRaises(llm.ModelError):
                self.client.preflight()

    def test_preflight_rejects_thinking_only_models_even_under_an_alias(self):
        client = llm.OllamaClient(ModelConfig(model="local-analyst:latest"))
        installed = {"models": [{"name": client.config.model, "size": 100}]}
        for details in (
            {"thinking": {"values": [True], "default": True}},
            {"thinking": {"values": ["low", "high"], "default": "low"}},
            {"model_info": {"general.architecture": "qwen3", "general.finetune": "Thinking"}},
            {"model_info": {"general.name": "Qwen3 4B Thinking 2507"}},
        ):
            with self.subTest(details=details), patch.object(llm, "request_json", side_effect=[
                    installed, {"capabilities": ["completion", "tools"], **details}]) as request:
                with self.assertRaisesRegex(llm.ModelError, "thinking-only") as raised:
                    client.preflight()
                self.assertIn("--ollama-model " + ModelConfig().model, str(raised.exception))
                self.assertIn("--snapshot latest", str(raised.exception))
                self.assertEqual([call.args[1] for call in request.call_args_list], ["/api/tags", "/api/show"])

    def test_preflight_allows_hybrid_and_non_thinking_models(self):
        installed = {"models": [{"name": self.client.config.model, "size": 100}]}
        for details in (
            {"thinking": {"values": [True, False]}},
            {"thinking": {"values": [False]}},
            {"model_info": {"general.name": "Qwen3 4B", "general.architecture": "qwen3"}},
            {"model_info": {"general.name": "Qwen3 4B Instruct 2507", "general.finetune": "Instruct"}},
        ):
            with self.subTest(details=details), patch.object(llm, "request_json", side_effect=[
                    installed, {"capabilities": ["completion", "tools"], **details}, {"version": "test"}]):
                self.assertEqual(self.client.preflight()["server_version"], "test")

    def test_preflight_records_the_context_window_and_rejects_a_larger_num_ctx(self):
        installed = {"models": [{"name": self.client.config.model, "size": 100}]}
        details = {"capabilities": ["completion", "tools"], "model_info": {"qwen3.context_length": 32768}}
        with patch.object(llm, "request_json", side_effect=[installed, details, {"version": "test"}]):
            self.assertEqual(self.client.preflight()["context_length"], 32768)
        client = llm.OllamaClient(ModelConfig(num_ctx=65536))
        with patch.object(llm, "request_json", side_effect=[installed, details]), \
             self.assertRaisesRegex(llm.ModelError, "--num-ctx 32768 or less"):
            client.preflight()

    def test_only_timeouts_connection_and_server_errors_are_marked_transient(self):
        cases = ((TimeoutError(), True), (URLError("refused"), True),
                 (HTTPError("http://localhost", 503, "", {}, io.BytesIO(b"")), True),
                 (HTTPError("http://localhost", 404, "", {}, io.BytesIO(b"")), False))
        for error, transient in cases:
            with self.subTest(error=error), patch.object(llm, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(llm.ModelError) as raised:
                    llm.request_json("http://localhost:11434", "/api/chat")
                self.assertEqual(raised.exception.transient, transient)

    def test_token_limit_discards_partial_tools_and_gives_safe_recovery_hint(self):
        partial = response("private partial content", [{"function": {
            "name": "describe_data", "arguments": {}}}], done_reason="length")
        with patch.object(llm, "request_json", return_value=partial) as request:
            with self.assertRaisesRegex(llm.ModelError, "response token limit") as raised:
                self.client.chat([], num_predict=1024)
        request.assert_called_once()
        message = str(raised.exception)
        self.assertIn("num_predict=1024", message)
        self.assertIn("--snapshot latest", message)
        self.assertNotIn("private partial content", message)
        self.assertNotIn("internal reasoning", message)

    def test_chat_is_local_deterministic_and_records_an_immutable_request(self):
        messages = [{"role": "user", "content": "question"}]
        calls = [{"type": "function", "function": {"index": 0, "name": "describe_data", "arguments": {}}}]
        with patch.object(llm, "request_json", return_value=response("", calls)) as request:
            reply = self.client.chat(messages, tools=[{"type": "function"}])
        payload = request.call_args.kwargs["payload"]
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["keep_alive"], "5m")
        self.assertEqual(request.call_args.kwargs["timeout"], 300)
        self.assertNotIn("format", payload)
        self.assertEqual(reply["tool_calls"], [{"name": "describe_data", "arguments": {}}])
        messages.append({"role": "user", "content": "later"})
        self.assertEqual(len(reply["request"]["messages"]), 1)
        self.assertNotIn("do not persist internal reasoning", json.dumps(reply))
        with patch.object(llm, "request_json", return_value=response("{}")) as request:
            self.client.chat(messages, format={"type": "object"})
        self.assertNotIn("tools", request.call_args.kwargs["payload"])
        self.assertEqual(request.call_args.kwargs["payload"]["format"], {"type": "object"})

    def test_incomplete_malformed_or_oversized_turns_fail(self):
        for reply in (response(done_reason="length"), {"done": False}, {"done": True, "done_reason": "stop"},
                      {"done": True, "done_reason": "stop", "message": {"content": 7}}):
            with self.subTest(reply=reply), patch.object(llm, "request_json", return_value=reply), \
                 self.assertRaises(llm.ModelError):
                self.client.chat([{"role": "user", "content": "x"}])
        with patch.object(llm, "request_json") as request, self.assertRaisesRegex(llm.ModelError, "too large"):
            self.client.chat([{"role": "user", "content": "x" * (llm.MAX_REQUEST_BYTES + 1)}])
        request.assert_not_called()
        malformed = response("", [{"function": "not-a-dict"}, "junk"])
        with patch.object(llm, "request_json", return_value=malformed):
            self.assertEqual(self.client.chat([])["tool_calls"], [{"name": None, "arguments": None}] * 2)


if __name__ == "__main__":
    unittest.main()
