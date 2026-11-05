"""End-to-end workflows through the command line, with fake MySQL and model services."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import runpy
import sys
import unittest
from unittest.mock import patch

from loan_analyst import PROJECT_ROOT, calculations, cli
from loan_analyst.snapshot import Snapshot, save_snapshot
from loan_analyst.ollama import ModelError
from loan_analyst.settings import ModelConfig
from tests.fixtures import (FakeConnection, ScriptedModel, calls, done, grounded, make_snapshot, scenario, services,
                            statement, temporary_directory)

ARGS = ["--cutoffs", "600,700", "--baseline", "700", "--min-outcomes", "1", "--start-month", "2013-01"]
FILES = ("report.md", "aggregates.json", "snapshot.json", "ollama_analysis.json")


class MissingModel:
    metadata = None

    def preflight(self):
        raise ModelError(f"{ModelConfig.model} is not installed locally")


class WorkflowTests(unittest.TestCase):
    def run_cli(self, argv, wired):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(argv, wired)
        return code, stderr.getvalue()

    def read(self, folder, name):
        return json.loads((folder / name).read_text(encoding="utf-8"))

    def test_report_keeps_configuration_scans_once_and_records_the_investigation(self):
        folder = temporary_directory(self, "report-test-") / "live"
        connection = FakeConnection(tables={"loan_inputs": "accepted", "request_inputs": "rejected"})
        model = ScriptedModel([calls(("compare_score_cutoffs", {"start_month": "2014-01"})), done(), grounded])
        wired = services(self, connection=connection, model=model)
        code, _ = self.run_cli(["report", "--host", "configured-host", "--port", "3307", "--user", "report-reader",
                                "--database", "custom_loans", "--accepted-table", "loan_inputs", "--rejected-table",
                                "request_inputs", "--query-timeout", "37", "--max-loss-rate", ".25", *ARGS,
                                "--output-dir", str(folder)], wired)
        self.assertEqual(code, 0)
        self.assertEqual((self.connected_with.host, self.connected_with.port, self.connected_with.user,
                          self.connected_with.database, self.connected_with.query_timeout),
                         ("configured-host", 3307, "report-reader", "custom_loans", 37))
        self.assertEqual(len([query for query, _ in connection.executed if "GROUP BY period" in query]), 2)
        self.assertEqual((len(connection.transactions), connection.rollbacks, connection.closed), (1, 1, True))
        self.assertTrue(connection.transactions[0]["readonly"])
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted(FILES))
        evidence, analysis = self.read(folder, "aggregates.json"), self.read(folder, "ollama_analysis.json")
        self.assertEqual(evidence["mode"], "historical_mysql")
        self.assertEqual(evidence["settings"]["database"], {"database": "custom_loans", "accepted_table": "loan_inputs",
                                                            "rejected_table": "request_inputs"})
        self.assertNotIn("configured-host", json.dumps(evidence))
        compare = next(call for call in evidence["calls"] if call["tool"] == "compare_score_cutoffs")
        self.assertEqual(compare["result"]["candidate_cutoff"], 600)
        snapshot = Snapshot.from_payload(self.read(folder, "snapshot.json"), "test")
        self.assertEqual(compare["result"], calculations.compare_cutoffs(snapshot, scenario(max_loss_rate=.25)))
        self.assertIn("`custom_loans`.`loan_inputs`", evidence["queries"]["accepted"])
        self.assertEqual((analysis["run_id"], analysis["source_evidence_sha256"]),
                         (evidence["run_id"], evidence["source_evidence_sha256"]))
        self.assertEqual(analysis["tool_results"][0]["arguments"], {"start_month": "2014-01"})
        report = (folder / "report.md").read_text(encoding="utf-8")
        for text in ("Historical MySQL aggregate analysis", "## Analyst summary", "## Loss by score band",
                     "## Performance by issue year", "## How this analysis was produced", "| t8 | analyst |",
                     "## Evidence cited", 'id="evidence-1"'):
            self.assertIn(text, report)

    def test_print_sql_and_sql_command_never_connect_or_write(self):
        folder = temporary_directory(self, "report-test-") / "must-not-exist"
        for argv in (["report", "--print-sql", "--output-dir", str(folder)], ["sql"]):
            wired = services(self)
            code, _ = self.run_cli(argv, wired)
            self.assertEqual(code, 0)
            queries = json.loads(wired.printed[0])
            self.assertEqual(set(queries), {"accepted", "rejected"})
            self.assertTrue(all("GROUP BY period, score" in value["sql"] and value["params"] == []
                                for value in queries.values()))
        self.assertFalse(folder.exists())

    def test_database_failures_are_sanitized_and_create_no_output(self):
        class ConnectorFailure(Exception):
            __module__ = "mysql.connector.errors"

        folder = temporary_directory(self, "report-test-") / "must-not-exist"
        for failure in (RuntimeError("borrower-secret credential-secret"),
                        ConnectorFailure("source-row-secret credential-secret")):
            wired = services(self, connection=FakeConnection(failure=failure), model=ScriptedModel([]))
            code, stderr = self.run_cli(["report", "--output-dir", str(folder)], wired)
            self.assertEqual(code, 1)
            self.assertIn("failed", stderr)
            self.assertNotIn("secret", stderr + "".join(wired.logged))
        self.assertFalse(folder.exists())

    def test_schema_problems_stay_actionable(self):
        wired = services(self, connection=FakeConnection(missing={"rejected": {"Risk_Score"}}), model=ScriptedModel([]))
        code, stderr = self.run_cli(["report", "--no-ai", "--output-dir",
                                     str(temporary_directory(self, "report-test-"))], wired)
        self.assertEqual(code, 1)
        self.assertIn("missing required columns: Risk_Score", stderr)

    def test_model_failure_preserves_outputs_but_keeps_the_snapshot_for_a_quick_rerun(self):
        folder = temporary_directory(self, "report-test-")
        for name in FILES:
            (folder / name).write_text("previous successful run", encoding="utf-8")
        connection = FakeConnection()
        wired = services(self, connection=connection, model=ScriptedModel([done(), {"content": "{"},
                                                                           {"content": "{"}]))
        code, stderr = self.run_cli(["report", *ARGS, "--output-dir", str(folder)], wired)
        self.assertEqual(code, 1)
        self.assertIn("failed evidence validation", stderr)
        self.assertTrue(connection.closed)
        for name in FILES:
            self.assertEqual((folder / name).read_text(encoding="utf-8"), "previous successful run")
        rerun = services(self, connection=None, model=ScriptedModel([done(), grounded]))
        rerun.snapshot_dir = wired.snapshot_dir
        code, _ = self.run_cli(["report", *ARGS, "--snapshot", "latest", "--output-dir", str(folder)], rerun)
        self.assertEqual(code, 0)
        self.assertIn("loaded from saved file", (folder / "report.md").read_text(encoding="utf-8"))
        self.assertTrue(any("MySQL is not queried" in line for line in rerun.logged))

    def test_missing_model_fails_before_any_database_scan(self):
        folder = temporary_directory(self, "report-test-") / "absent"
        wired = services(self, connection=None, model=MissingModel())
        code, stderr = self.run_cli(["report", "--output-dir", str(folder)], wired)
        self.assertEqual(code, 1)
        self.assertIn("not installed", stderr)
        self.assertFalse(folder.exists())

    def test_no_ai_is_explicit_and_still_uses_mysql(self):
        folder = temporary_directory(self, "report-test-")
        (folder / "ollama_analysis.json").write_text("stale model results", encoding="utf-8")
        wired = services(self, connection=FakeConnection(), model=MissingModel())
        code, _ = self.run_cli(["report", "--no-ai", *ARGS, "--output-dir", str(folder)], wired)
        self.assertEqual(code, 0)
        evidence, model = self.read(folder, "aggregates.json"), self.read(folder, "ollama_analysis.json")
        self.assertEqual(model["status"], "disabled")
        self.assertEqual((model["run_id"], model["source_evidence_sha256"]),
                         (evidence["run_id"], evidence["source_evidence_sha256"]))
        self.assertNotIn("demo", evidence["settings"]["scenario"])
        self.assertIn("Disabled explicitly with `--no-ai`", (folder / "report.md").read_text(encoding="utf-8"))

    def test_sections_can_be_chosen_and_unknown_ones_rejected(self):
        folder = temporary_directory(self, "report-test-")
        wired = services(self, connection=FakeConnection())
        code, _ = self.run_cli(["report", "--no-ai", *ARGS, "--sections", "cutoffs,coverage", "--output-dir",
                                str(folder)], wired)
        self.assertEqual(code, 0)
        report = (folder / "report.md").read_text(encoding="utf-8")
        self.assertLess(report.index("## Accepted loans"), report.index("## Coverage and exclusions"))
        self.assertNotIn("## Loss by score band", report)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["report", "--sections", "cutoffs,gossip"], services(self))
        self.assertEqual(raised.exception.code, 2)

    def test_invalid_arguments_and_unsupported_demo_mode_are_usage_errors(self):
        for argv in (["report", "--max-loss-rate", "nan"], ["report", "--demo"], ["report", "--port", "0"],
                     ["ask"], ["report", "--rejected-loss-rate", ".1"], ["chat", "--ollama-url", "https://ollama.com"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(argv, services(self))
            self.assertEqual(raised.exception.code, 2)

    def test_paths_are_project_relative(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["report"]).output_dir, PROJECT_ROOT / "outputs" / "framework_report")
        self.assertEqual(parser.parse_args(["report", "--output-dir", "outputs/custom"]).output_dir,
                         PROJECT_ROOT / "outputs" / "custom")
        self.assertEqual(parser.parse_args(["ask", "q"]).output_dir, PROJECT_ROOT / "outputs" / "ask")

    def test_ask_prints_a_cited_answer_and_saves_a_transcript(self):
        folder = temporary_directory(self, "ask-test-")
        model = ScriptedModel([calls(("segment_performance", {"dimension": "grade"})), done(),
                               lambda payload: grounded(payload, answer=[statement(
                                   "Grade C has a 40.00% principal-loss proxy.", "t2.grade_c")])])
        wired = services(self, connection=FakeConnection(), model=model)
        code, _ = self.run_cli(["ask", "Which grade loses most?", *ARGS, "--output-dir", str(folder)], wired)
        self.assertEqual(code, 0)
        printed = "\n".join(wired.printed)
        self.assertIn("Grade C has a 40.00% principal-loss proxy. [1]", printed)
        self.assertIn("[1] t2.grade_c: Grade C:", printed)
        self.assertIn("Tools the analyst ran: t2 segment_performance(dimension=grade)", printed)
        transcript = json.loads(next(folder.glob("ask-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(transcript["turns"][0]["question"], "Which grade loses most?")
        self.assertIn("Which grade loses most?", next(folder.glob("ask-*.md")).read_text(encoding="utf-8"))

    def test_chat_answers_several_questions_and_survives_a_failed_turn(self):
        folder = temporary_directory(self, "chat-test-")
        model = ScriptedModel([done(), grounded, done(), {"content": "{"}, {"content": "{"}, done(), grounded])
        wired = services(self, connection=FakeConnection(), model=model,
                         lines=["", "/tools", "First question?", "Broken question?", "Third question?", "/quit"])
        code, _ = self.run_cli(["chat", *ARGS, "--output-dir", str(folder)], wired)
        self.assertEqual(code, 0)
        printed = "\n".join(wired.printed)
        self.assertIn("Loan analyst ready", printed)
        self.assertIn("segment_performance:", printed)
        self.assertIn("could not produce a validated answer", printed)
        transcript = json.loads(next(folder.glob("chat-*.json")).read_text(encoding="utf-8"))
        self.assertEqual([turn["question"] for turn in transcript["turns"]], ["First question?", "Third question?"])

    def test_render_requires_recorded_and_grounded_review_edits(self):
        folder = temporary_directory(self, "render-test-")
        wired = services(self, connection=FakeConnection(), model=ScriptedModel([done(), grounded]))
        self.assertEqual(self.run_cli(["report", *ARGS, "--output-dir", str(folder)], wired)[0], 0)
        analysis = self.read(folder, "ollama_analysis.json")
        analysis["answer"]["answer"][0]["text"] = "Edited: higher cutoffs retain fewer loans."
        (folder / "ollama_analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        code, stderr = self.run_cli(["render", str(folder)], services(self))
        self.assertEqual(code, 1)
        self.assertIn("review", stderr)
        analysis["review"] = {"reviewer": "analyst", "notes": "Clarified the headline."}
        analysis["answer"]["answer"][0]["text"] = "Edited: losses fell 99% at higher cutoffs."
        (folder / "ollama_analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        code, stderr = self.run_cli(["render", str(folder)], services(self))
        self.assertIn("fails evidence validation", stderr)
        analysis["answer"]["answer"][0]["text"] = "Edited: higher cutoffs retain fewer loans."
        (folder / "ollama_analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        self.assertEqual(self.run_cli(["render", str(folder)], services(self))[0], 0)
        report = (folder / "report.md").read_text(encoding="utf-8")
        self.assertIn("Edited: higher cutoffs retain fewer loans.", report)
        self.assertIn("Wording reviewed", report)

    def test_config_file_sets_defaults_flags_override_and_passwords_are_refused(self):
        folder = temporary_directory(self, "config-test-")
        config = folder / "team.toml"
        config.write_text('[scenario]\ncutoffs = [600, 700]\nbaseline = 700\nmin_outcomes = 1\n'
                          'start_month = "2013-01"\nmax_loss_rate = 0.1\n', encoding="utf-8")
        wired = services(self)
        code, _ = self.run_cli(["--config", str(config), "call", "compare_score_cutoffs", "{}", "--snapshot",
                                str(self.saved_snapshot(wired))], wired)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(wired.printed[0])["result"]["candidate_cutoff"], 700)
        wired = services(self)
        self.run_cli(["--config", str(config), "call", "compare_score_cutoffs", "{}", "--max-loss-rate", "0.25",
                      "--snapshot", str(self.saved_snapshot(wired))], wired)
        self.assertEqual(json.loads(wired.printed[0])["result"]["candidate_cutoff"], 600)
        config.write_text('[model]\nmodel = "qwen3:8b"\nnum_ctx = 8192\nkeep_alive = "10m"\nmax_tool_rounds = 2\n',
                          encoding="utf-8")
        wired = services(self, model=ScriptedModel([done(), grounded]))
        self.run_cli(["--config", str(config), "ask", "Question?", "--snapshot", str(self.saved_snapshot(wired)),
                      "--max-tool-calls", "3", "--output-dir", str(folder)], wired)
        self.assertEqual((self.model_config.model, self.model_config.num_ctx, self.model_config.keep_alive,
                          self.model_config.max_tool_rounds, self.model_config.max_tool_calls),
                         ("qwen3:8b", 8192, "10m", 2, 3))
        config.write_text('[database]\npassword = "hunter2"\n', encoding="utf-8")
        code, stderr = self.run_cli(["--config", str(config), "sql"], services(self))
        self.assertEqual(code, 2)
        self.assertNotIn("hunter2", stderr)

    def saved_snapshot(self, wired):
        return save_snapshot(make_snapshot(), wired.snapshot_dir)

    def test_tool_commands_print_machine_readable_json(self):
        wired = services(self)
        self.assertEqual(self.run_cli(["tools"], wired)[0], 0)
        self.assertEqual(len(json.loads(wired.printed[0])["tools"]), 5)
        wired = services(self)
        code, _ = self.run_cli(["call", "compare_score_cutoffs", '{"sql": "SELECT 1"}', "--snapshot",
                                str(self.saved_snapshot(wired))], wired)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(wired.printed[0])["error"]["code"], "invalid_arguments")

    def test_compatibility_script_runs_the_report_command(self):
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["generate_framework_report.py", "--print-sql"]), redirect_stdout(stdout), \
             self.assertRaises(SystemExit) as raised:
            runpy.run_path(str(PROJECT_ROOT / "generate_framework_report.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("GROUP BY period, score", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
