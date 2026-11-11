"""The evaluation harness, with a scripted model and a saved fixture snapshot."""

from contextlib import redirect_stderr
import io
import json
import unittest

from loan_analyst import cli, evals
from loan_analyst.settings import SettingsError
from loan_analyst.snapshot import save_snapshot
from tests.fixtures import (ScriptedModel, auto, broken, calls, done, grounded, make_snapshot, services,
                            statement, temporary_directory, verdicts)

ARGS = ["--cutoffs", "600,700", "--baseline", "700", "--min-outcomes", "1", "--start-month", "2013-01"]

SUITE = """
[[case]]
id = "grade"
kind = "ask"
question = "Which grade loses most?"
expect_tools = [{ name = "segment_performance", arguments = { dimension = "grade" } }]
forbid_tools = ["rejected_applications"]
expect_cited = ["segment_performance:grade_"]
forbid_text = ['\\bapprov']

[[case]]
id = "definition"
kind = "ask"
question = "What is the loss proxy?"
expect_cited = ["def:loss_proxy", "compare_score_cutoffs:"]

[[case]]
id = "broken"
kind = "ask"
question = "A question the model answers with invalid JSON"
"""


class EvalTests(unittest.TestCase):
    def setUp(self):
        self.folder = temporary_directory(self, "eval-test-")
        self.suite = self.folder / "cases.toml"
        self.suite.write_text(SUITE, encoding="utf-8")

    def run_eval(self, model, *extra, snapshot=True):
        wired = services(self, model=model)
        argv = ["eval", "--suite", str(self.suite), *ARGS, "--no-critic", "--output-dir",
                str(self.folder / "out"), *extra]
        if snapshot:
            argv += ["--snapshot", str(save_snapshot(make_snapshot(), wired.snapshot_dir))]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(argv, wired)
        return wired, code, stderr.getvalue()

    def replies(self):
        grade = lambda payload: grounded(payload, answer=[statement(
            "Grade C has a 40.00% principal-loss proxy.", "t2.grade_c")])
        return [
            # grade: one invalid request, the expected call, an unrelated call, then a grounded answer
            calls(("segment_performance", {"dimension": "colour"}), ("segment_performance", {"dimension": "grade"}),
                  ("describe_data", {"end_month": "2015-12"})), done(), grade,
            # definition: answers from a definition, so the cutoff citation it also expects is missing
            done(), grounded,
            # broken: the answer never validates
            done(), broken(), broken(),
        ]

    def test_cases_are_scored_summarized_and_saved(self):
        wired, code, _ = self.run_eval(ScriptedModel(self.replies()))
        self.assertEqual(code, 0)
        folder = next((self.folder / "out").iterdir())
        self.assertEqual(folder.name, "20261103T103000Z-qwen3-4b-instruct-2507-q4_K_M")
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        grade, definition, failed = results["cases"]

        self.assertTrue(grade["passed"])
        self.assertEqual((grade["tool_calls"], grade["invalid_tool_calls"], grade["expected_tools_found"],
                          grade["relevant_tool_calls"], grade["model_calls"]), (2, 1, 1, 1, 3))
        self.assertEqual(definition["failures"], ["no cited fact matches compare_score_cutoffs:"])
        self.assertEqual((failed["answered"], failed["error"]["type"]), (False, "AnalysisError"))

        summary = results["summary"]
        self.assertEqual((summary["cases"], summary["pass_rate"], summary["answered_rate"]), (3, 1 / 3, 2 / 3))
        self.assertEqual((summary["tool_recall"], summary["tool_precision"]), (1.0, 0.5))
        self.assertEqual(summary["invalid_tool_call_rate"], 1 / 3)
        self.assertEqual((results["snapshot_id"], results["prompt_version"]) ,
                         (make_snapshot().snapshot_id, evals.prompts.VERSION))

        card = (folder / "scorecard.md").read_text(encoding="utf-8")
        self.assertEqual(card, wired.printed[0])
        self.assertIn("| Cases passed | 33.3% |", card)
        self.assertIn("| `definition` | FAIL |", card)
        self.assertEqual(sorted(path.name for path in folder.glob("case-*.json")),
                         ["case-definition-1.json", "case-grade-1.json"])

    def test_each_case_starts_from_a_fresh_ledger_and_repeats_are_counted(self):
        model = ScriptedModel(self.replies() * 2)
        wired, code, _ = self.run_eval(model, "--repeat", "2")
        self.assertEqual(code, 0)
        results = json.loads(next((self.folder / "out").glob("*/results.json")).read_text(encoding="utf-8"))
        self.assertEqual([score["attempt"] for score in results["cases"]], [1, 1, 1, 2, 2, 2])
        self.assertTrue(results["cases"][3]["passed"])  # t2 is the grade call again, not t4

    def test_baseline_shows_direction_of_change(self):
        self.run_eval(ScriptedModel(self.replies()))
        baseline = next((self.folder / "out").glob("*/results.json"))
        better = self.replies()
        better[3:5] = [done(), lambda payload: grounded(payload, findings=[
            statement("The loss proxy uses complete terminal loans.", "def.loss_proxy"),
            statement("The cohort and baseline are fixed for this comparison.", "t1.snapshot")])]
        wired, code, _ = self.run_eval(ScriptedModel(better), "--compare-with", str(baseline))
        self.assertEqual(code, 0)
        self.assertIn("| Baseline | Change |", wired.printed[0])
        self.assertIn("same", wired.printed[0])

    def test_eval_never_reads_mysql_and_rejects_bad_suites(self):
        _, code, stderr = self.run_eval(ScriptedModel([]), snapshot=False)
        self.assertEqual(code, 1)
        self.assertIn("--snapshot latest", stderr)

        for text, message in (("", "at least one"),
                              ('[[case]]\nid = "a"\nkind = "poll"\n', "kind must be"),
                              ('[[case]]\nid = "a"\nkind = "ask"\n', "needs a question"),
                              ('[[case]]\nid = "a"\nkind = "report"\nexpect_tools = [{ name = "execute_sql" }]\n',
                               "known tool name"),
                              ('[[case]]\nid = "a"\nkind = "report"\nexpect_cited = ["sql:x"]\n', "expect_cited"),
                              ('[[case]]\nid = "a"\nkind = "report"\nforbid_text = ["("]\n', "regular expressions"),
                              ('[[case]]\nid = "a"\nkind = "report"\nscore = 1\n', "unknown key score")):
            self.suite.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaisesRegex(SettingsError, message):
                evals.load_suite(self.suite)

    def test_report_cases_run_the_topic_workflow(self):
        self.suite.write_text('[[case]]\nid = "standard"\nkind = "report"\nmin_findings = 2\n'
                              'expect_cited = ["def:"]\n', encoding="utf-8")
        model = ScriptedModel([], default=auto)
        _, code, _ = self.run_eval(model, "--tool-mode", "schema", "--critic")
        self.assertEqual(code, 0)
        folder = next((self.folder / "out").iterdir())
        results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["cases"][0]["failures"], [])
        self.assertFalse(model.required_tools)  # schema mode needs no native tool calling
        artifact = json.loads((folder / "case-standard-1.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["agent"], {"flow": "report", "tool_mode": "schema", "critic": True})
        self.assertTrue(artifact["critic_reviews"])

    def test_critic_cases_compare_the_verdict_with_the_seeded_expectation(self):
        self.suite.write_text(
            '[[case]]\nid = "fair"\nkind = "critic"\nstatement = "Losses use resolved loans."\n'
            'cite = ["loss_proxy"]\nexpect = "supported"\n'
            '[[case]]\nid = "missed"\nkind = "critic"\nstatement = "Losses will fall next year."\n'
            'cite = ["loss_proxy"]\nexpect = "flagged"\n'
            '[[case]]\nid = "caught"\nkind = "critic"\nstatement = "Rejected applicants would repay."\n'
            'cite = ["rejected_scores"]\nexpect = "flagged"\n', encoding="utf-8")
        model = ScriptedModel([auto, auto, lambda payload: verdicts(payload, s1="overreach")])
        wired, code, _ = self.run_eval(model)
        self.assertEqual(code, 0)
        self.assertIn("[def.rejected_scores] Rejected Risk_Score", model.requests[2]["messages"][-1]["content"])
        results = json.loads(next((self.folder / "out").glob("*/results.json")).read_text(encoding="utf-8"))
        self.assertEqual([(score["verdict"], score["passed"]) for score in results["cases"]],
                         [("supported", True), ("supported", False), ("flagged", True)])
        self.assertEqual(results["summary"]["critic_agreement"], 2 / 3)
        self.assertIn("critic said supported, expected flagged", wired.printed[0])
        self.suite.write_text('[[case]]\nid = "a"\nkind = "critic"\nstatement = "x"\ncite = ["made_up"]\n'
                              'expect = "flagged"\n', encoding="utf-8")
        with self.assertRaisesRegex(SettingsError, "critic case needs"):
            evals.load_suite(self.suite)

    def test_the_tracked_suite_is_valid(self):
        cases = evals.load_suite(evals.DEFAULT_SUITE)
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual({case["kind"] for case in cases}, {"ask", "report", "critic"})


if __name__ == "__main__":
    unittest.main()
