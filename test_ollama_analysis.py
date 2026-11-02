"""Offline model boundary tests. Never contacts MySQL or a running Ollama server."""

import hashlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import generate_framework_report as bridge
import ollama_analysis as ai
import score_cutoff_simulation as simulation
from test_score_cutoff_simulation import fixture


def analysis_fixture():
    return {key: [{"text": "Incomplete outcomes can bias the loss comparison.",
                   "evidence_ids": ["loss_definition", "source_limits"]}]
            for key in ai.SECTIONS}


def response_fixture(analysis=None):
    return {"done": True, "done_reason": "stop", "model": "qwen3:4b",
            "message": {"content": json.dumps(analysis or analysis_fixture()),
                        "thinking": "do not persist internal reasoning"},
            "prompt_eval_count": 80, "eval_count": 30}


class OllamaAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.args = bridge.parse_args(["--cutoffs", "600,700", "--baseline", "700", "--min-outcomes", "1"])
        self.results = simulation.analyze(fixture(), self.args)
        self.facts = ai.build_facts(self.results, self.args)
        self.metadata = {"model": "qwen3:4b", "model_digest": "test-digest"}

    def test_only_loopback_endpoints_are_accepted(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"):
            self.assertEqual(ai.local_url(url), url.rstrip("/"))
        for url in ("https://ollama.com", "http://example.com", "http://localhost.evil", "file:///tmp",
                    "http://user:secret@localhost:11434", "http://localhost:11434/api",
                    "http://localhost:11434?target=evil", "http://localhost:99999", "http://192.168.1.2"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ai.local_url(url)

    def test_http_request_disables_proxy_and_redirects_and_bounds_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"done": true}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(ai, "build_opener", return_value=opener) as build:
            self.assertEqual(ai.request_json(self.args.ollama_url, "/api/chat", payload={"model": "qwen3:4b"}, timeout=17),
                             {"done": True})
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], ai._NoRedirect)
        with self.assertRaises(ai.AnalysisError):
            handlers[1].redirect_request(None, None, 302, "", {}, "https://example.com")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(json.loads(request.data), {"model": "qwen3:4b"})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 17)
        response.read.assert_called_once_with(2_000_001)

    def test_http_errors_are_actionable_and_do_not_echo_response_bodies(self):
        errors = [TimeoutError("private value"), URLError("private value"),
                  HTTPError("http://localhost", 500, "private value", {}, io.BytesIO(b"private value"))]
        for error in errors:
            with patch.object(ai, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(ai.AnalysisError) as raised:
                    ai.request_json(self.args.ollama_url, "/api/chat")
                self.assertNotIn("private value", str(raised.exception))

    def test_preflight_requires_an_installed_local_model(self):
        installed = {"models": [{"name": "qwen3:4b", "size": 100, "digest": "d"}]}
        with patch.object(ai, "request_json", side_effect=[installed, {"version": "test"}]):
            self.assertEqual(ai.preflight(self.args)["model_digest"], "d")
        for inventory in ({"models": []}, {"models": None},
                          {"models": [{"name": "qwen3:4b", "size": 100, "remote_host": "cloud"}]}):
            with patch.object(ai, "request_json", return_value=inventory):
                with self.assertRaises(ai.AnalysisError):
                    ai.preflight(self.args)

    def test_facts_preserve_unavailable_metrics_and_exclude_private_fields(self):
        self.results["password"] = "private-marker"
        self.results["borrower"] = "private-marker"
        self.results["quality"]["accepted"]["raw_rows"] = "private-marker"
        self.results["scenarios"][0]["loss_rate"] = None
        facts = ai.build_facts(self.results, self.args)
        self.assertIn("principal-loss proxy N/A", facts["accepted_600"])
        self.assertIn("no cutoff is selected", facts["selection"])
        self.assertIn("invalid scores", facts["quality_accepted"])
        self.assertNotIn("private-marker", json.dumps(facts))
        self.assertNotIn("SELECT", json.dumps(facts))

    def test_comparison_facts_separate_loss_from_financial_completeness(self):
        self.args.baseline = 600
        self.results = simulation.analyze(fixture(), self.args)
        self.results["scenarios"][-1]["loss_data_coverage"] = .8
        facts = ai.build_facts(self.results, self.args)
        self.assertIn("fewer accepted loans", facts["comparison_high"])
        self.assertIn("loss proxy is lower", facts["comparison_high"])
        self.assertIn("coverage at the highest cutoff is lower", facts["comparison_coverage"])
        self.assertIn("Financial coverage is completeness", facts["comparison_coverage"])
        self.assertIn("invalid-amount exclusions are 20.0000%", facts["quality_comparison"])
        self.assertIn("do not distinguish these reasons", facts["quality_comparison"])

    def test_near_equal_loss_and_empty_cohort_are_not_meaningful_changes(self):
        self.results["scenarios"][0]["loss_rate_change_pp"] = .0001
        facts = ai.build_facts(self.results, self.args)
        self.assertIn("unchanged at the displayed precision", facts["comparison_low"])
        empty = simulation.analyze({"accepted": [], "rejected": []}, self.args)
        facts = ai.build_facts(empty, self.args)
        self.assertIn("unavailable", facts["comparison_high"])
        self.assertIn("unavailable", facts["comparison_coverage"])
        self.assertIn("unavailable", facts["quality_comparison"])

    def test_invalid_structure_numbers_and_evidence_are_rejected(self):
        invalid = [[], {}, {**analysis_fixture(), "extra": []}]
        for item in ({"text": "Invented 99% loss", "evidence_ids": ["loss_definition"]},
                     {"text": "The cutoff rejects more loans.", "evidence_ids": ["comparison_high"]},
                     {"text": "An interpretation.", "evidence_ids": ["unknown"]},
                     {"text": "An interpretation.", "evidence_ids": ["cohort", "cohort"]},
                     {"text": "", "evidence_ids": ["cohort"]},
                     {"text": "An interpretation.", "evidence_ids": []}):
            analysis = analysis_fixture()
            analysis["summary"] = [item]
            invalid.append(analysis)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ai.AnalysisError):
                ai.validate_analysis(value, self.facts)
        for value in ('{"a":NaN}', '{"a":1,"a":2}'):
            with self.assertRaises(ValueError):
                ai.read_json(value)

    def test_quoted_numbers_must_match_cited_facts_and_units(self):
        facts = {"loss_definition": "Loss proxy 13.80%; funded amount $34,000,792,025.",
                 "source_limits": "There are 2,260,179 retained loans."}
        analysis = analysis_fixture()
        analysis["summary"][0]["text"] = "The loss proxy is 13.8% across 2,260,179 retained loans."
        ai.validate_analysis(analysis, facts)
        for text in ("The loss is 14%.", "The loss is $13.80.", "The funded amount is $34 billion."):
            analysis["summary"][0]["text"] = text
            with self.assertRaisesRegex(ai.AnalysisError, "numeric values or units"):
                ai.validate_analysis(analysis, facts)
        analysis["summary"][0] = {"text": "There are 2,260,179 loans.", "evidence_ids": ["loss_definition"]}
        with self.assertRaises(ai.AnalysisError):
            ai.validate_analysis(analysis, facts)

    def test_generation_is_structured_bounded_and_auditable(self):
        with patch.object(ai, "request_json", return_value=response_fixture()) as request:
            artifact = ai.generate_analysis(self.results, self.args, self.metadata)
        request.assert_called_once()
        payload = request.call_args.kwargs["payload"]
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertNotIn("tools", payload)
        self.assertTrue(artifact["facts"].keys() <= self.facts.keys())
        self.assertTrue(all(self.facts[key] == value for key, value in artifact["facts"].items()))
        self.assertEqual(json.loads(payload["messages"][1]["content"])["facts"], artifact["facts"])
        self.assertEqual(artifact["analysis"], analysis_fixture())
        self.assertNotIn("do not persist internal reasoning", json.dumps(artifact))
        request_hash = hashlib.sha256(json.dumps(artifact["request"], sort_keys=True,
                                                ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        self.assertEqual(artifact["request_sha256"], request_hash)
        rendered = ai.render_analysis(artifact)
        self.assertIn("#evidence-loss-definition", rendered)
        self.assertIn('id="evidence-loss-definition"', rendered)
        self.assertIn("model-generated", rendered)

    def test_only_one_validation_retry_and_truncated_output_fails(self):
        bad = response_fixture()
        bad["message"]["content"] = '{"invalid": true}'
        with patch.object(ai, "request_json", side_effect=[bad, response_fixture()]) as request:
            artifact = ai.generate_analysis(self.results, self.args, self.metadata)
        self.assertEqual(artifact["attempts"], 2)
        self.assertEqual(request.call_count, 2)
        with patch.object(ai, "request_json", return_value=bad) as request:
            with self.assertRaises(ai.AnalysisError):
                ai.generate_analysis(self.results, self.args, self.metadata)
        self.assertEqual(request.call_count, 2)
        truncated = response_fixture()
        truncated["done_reason"] = "length"
        with patch.object(ai, "request_json", return_value=truncated) as request:
            with self.assertRaisesRegex(ai.AnalysisError, "incomplete"):
                ai.generate_analysis(self.results, self.args, self.metadata)
        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
