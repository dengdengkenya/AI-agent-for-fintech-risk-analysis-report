"""Run plumbing: the model-reply journal, the trace and the call budget. No services."""

import unittest

from loan_analyst.ollama import ModelError
from loan_analyst.runtime import (BudgetExceeded, Journal, ResumeError, Run, Tracer, format_trace,
                                  identity_differences, read_trace)
from tests.fixtures import ScriptedModel, temporary_directory

QUESTION = [{"role": "user", "content": "question"}]


class FlakyModel(ScriptedModel):
    """Raises the queued errors first, then replies normally."""

    def __init__(self, errors, replies):
        super().__init__(replies)
        self.errors = list(errors)

    def chat(self, messages, **options):
        if self.errors:
            raise self.errors.pop(0)
        return super().chat(messages, **options)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self, "runs-test-")

    def journal(self, **identity):
        return Journal.create(self.directory, "run-1", "report", identity or {"model": "m"}, snapshot_id="snap")

    def test_saved_replies_replay_in_order_and_survive_a_torn_final_line(self):
        journal = self.journal()
        journal.append("investigate", "key-1", {"content": "DONE"})
        journal.append("compose", "key-2", {"content": "{}"})
        with open(journal.path, "a", encoding="utf-8") as stream:
            stream.write('{"stage": "compose", "key": "key-3", "rep')  # the process died mid-write

        loaded = Journal.find(self.directory)
        self.assertEqual((loaded.run_id, loaded.header["snapshot_id"]), ("run-1", "snap"))
        self.assertEqual(loaded.replay("key-1"), {"content": "DONE"})
        self.assertEqual(loaded.replay("key-2"), {"content": "{}"})
        self.assertIsNone(loaded.replay("key-3"))

    def test_a_diverging_call_discards_every_later_saved_reply(self):
        journal = self.journal()
        for key in ("key-1", "key-2", "key-3"):
            journal.append("stage", key, {"content": key})

        loaded = Journal.load(journal.path)
        self.assertEqual(loaded.replay("key-1"), {"content": "key-1"})
        self.assertIsNone(loaded.replay("changed"))
        self.assertIsNone(loaded.replay("key-3"))  # never reuse a reply from after the divergence
        loaded.append("stage", "changed", {"content": "new"})

        again = Journal.load(journal.path)
        self.assertEqual(again.replay("key-1"), {"content": "key-1"})
        self.assertEqual(again.replay("changed"), {"content": "new"})

    def test_missing_unreadable_and_escaping_references_are_refused(self):
        with self.assertRaisesRegex(ResumeError, "No interrupted run"):
            Journal.find(self.directory)
        with self.assertRaisesRegex(ResumeError, "folder name"):
            Journal.find(self.directory, "../elsewhere")
        journal = self.journal()
        journal.path.write_text("not a journal\n", encoding="utf-8")
        with self.assertRaisesRegex(ResumeError, "unreadable"):
            Journal.find(self.directory, "run-1")

    def test_identity_differences_name_the_changed_fields(self):
        saved = {"model": "a", "scenario": {"baseline": 660}, "prompt_version": "v3"}
        self.assertEqual(identity_differences(saved, dict(saved)), [])
        self.assertEqual(identity_differences(saved, {**saved, "model": "b", "extra": 1}), ["extra", "model"])


class RunTests(unittest.TestCase):
    def test_transient_failures_are_retried_once_and_others_are_not(self):
        model = FlakyModel([ModelError("timed out", transient=True)], [{"content": "ok"}])
        model.preflight()
        run = Run(model)
        reply, replayed = run.chat("investigate", QUESTION)
        self.assertEqual((reply["content"], replayed), ("ok", False))
        self.assertTrue(run.tracer.spans[0]["attributes"]["retried"])

        for errors in ([ModelError("token limit")], [ModelError("down", transient=True)] * 2):
            failing = Run(FlakyModel(errors, [{"content": "never"}]))
            with self.subTest(errors=errors), self.assertRaises(ModelError):
                failing.chat("investigate", QUESTION)
            self.assertEqual(failing.tracer.spans[0]["status"], "error")
            self.assertEqual(failing.tracer.spans[0]["error_type"], "ModelError")

    def test_budget_counts_real_calls_only_and_resets_per_question(self):
        directory = temporary_directory(self, "runs-test-")
        journal = Journal.create(directory, "run-1", "report", {})
        first = Run(ScriptedModel([{"content": "one"}]), journal=journal, max_model_calls=1)
        first.chat("stage", QUESTION)
        with self.assertRaisesRegex(BudgetExceeded, "limit of 1 model calls"):
            first.chat("stage", [{"role": "user", "content": "another"}])
        first.reset_budget()
        self.assertEqual(first.model_calls, 0)

        model = ScriptedModel([{"content": "two"}])
        resumed = Run(model, journal=Journal.load(journal.path), max_model_calls=1)
        reply, replayed = resumed.chat("stage", QUESTION)
        self.assertEqual((reply["content"], replayed, resumed.model_calls, resumed.replayed), ("one", True, 0, 1))
        self.assertEqual(model.requests, [])
        self.assertEqual(resumed.chat("stage", [{"role": "user", "content": "new"}])[0]["content"], "two")

    def test_requests_with_different_options_are_different_calls(self):
        directory = temporary_directory(self, "runs-test-")
        journal = Journal.create(directory, "run-1", "report", {})
        Run(ScriptedModel([{"content": "plain"}]), journal=journal).chat("stage", QUESTION)
        model = ScriptedModel([{"content": "structured"}])
        reply, replayed = Run(model, journal=Journal.load(journal.path)).chat(
            "stage", QUESTION, format={"type": "object"})
        self.assertEqual((reply["content"], replayed), ("structured", False))


class TracerTests(unittest.TestCase):
    def test_spans_nest_record_errors_and_round_trip_as_a_timeline(self):
        tracer = Tracer(clock=lambda: "2026-11-03T10:30:00+00:00")
        with tracer.span("analyst"):
            with tracer.span("chat compose", **{"gen_ai.usage.input_tokens": 100}) as attributes:
                attributes["gen_ai.usage.output_tokens"] = 20
            with self.assertRaises(ValueError), tracer.span("execute_tool x"):
                raise ValueError("secret detail")

        trace_id, spans = read_trace(tracer.to_jsonl("run-1"))
        self.assertEqual(trace_id, "run-1")
        self.assertEqual([(span["name"], span["parent_id"]) for span in spans],
                         [("analyst", None), ("chat compose", "s1"), ("execute_tool x", "s1")])
        self.assertEqual((spans[2]["status"], spans[2]["error_type"]), ("error", "ValueError"))
        self.assertNotIn("secret detail", tracer.to_jsonl("run-1"))

        timeline = format_trace(trace_id, spans)
        self.assertIn("  chat compose  (100 in / 20 out tokens)", timeline)
        self.assertIn("ERROR ValueError", timeline)
        with self.assertRaises(ValueError):
            read_trace('{"format": "other"}\n')


if __name__ == "__main__":
    unittest.main()
