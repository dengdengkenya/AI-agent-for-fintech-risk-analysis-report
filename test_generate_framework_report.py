"""Offline report bridge checks, using synthetic data and fake database reads."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import generate_framework_report as bridge
import score_cutoff_simulation as simulation
from test_score_cutoff_tools import FakeConnection, FakeCursor


class ConfiguredCursor(FakeCursor):
    """Recognize the configured test table names while recording actual queries."""

    def execute(self, query, params=()):
        if "information_schema.COLUMNS" in query:
            self.connection.executed.append((query, params))
            kind = self.connection.aliases[params[1]]
            self.current = [{"COLUMN_NAME": field} for field in simulation.REQUIRED[kind]]
        elif "GROUP BY scope, bucket" in query:
            self.connection.executed.append((query, params))
            kind = next(kind for table, kind in self.connection.aliases.items()
                        if f"`{table}`" in query)
            self.current = self.connection.aggregates[kind]
        else:
            super().execute(query, params)


class ConfiguredConnection(FakeConnection):
    aliases = {"loan_inputs": "accepted", "request_inputs": "rejected"}

    def cursor(self, dictionary=False):
        if not dictionary:
            raise AssertionError("Expected dictionary cursor")
        cursor = ConfiguredCursor(self)
        self.cursors.append(cursor)
        return cursor


class FrameworkReportTests(unittest.TestCase):
    def output_directory(self):
        root = simulation.PROJECT_ROOT / "outputs"
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="framework-report-test-", dir=root)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_demo_writes_matching_evidence_and_report_without_credentials(self):
        directory = self.output_directory() / "demo"
        argv = ["--demo", "--output-dir", str(directory), "--cutoffs", "600,700",
                "--baseline", "700", "--min-outcomes", "1", "--max-loss-rate", ".08",
                "--assume-comparable-scores", "--rejected-loss-rate", ".15", "--take-up-rate", ".25"]
        stdout = io.StringIO()
        with patch.dict("os.environ", {"MYSQL_PASSWORD": "must-not-appear-in-artifacts"}), \
             patch.object(bridge, "connect_with_password", side_effect=AssertionError("No database in demo")), \
             patch.object(bridge, "getpass", side_effect=AssertionError("No password prompt in demo")), \
             redirect_stdout(stdout):
            self.assertEqual(bridge.main(argv), 0)
        report_text = (directory / "report.md").read_text(encoding="utf-8")
        evidence_text = (directory / "aggregates.json").read_text(encoding="utf-8")
        evidence = json.loads(evidence_text)
        args = simulation.parse_args(argv)
        expected_aggregates = simulation.demo_aggregates(args)
        expected_results = simulation.analyze(expected_aggregates, args)
        self.assertEqual(evidence["aggregates"], expected_aggregates)
        self.assertEqual(evidence["results"], expected_results)
        self.assertEqual(evidence["mode"], "synthetic_demo")
        self.assertEqual(evidence["queries"], {})
        self.assertTrue(evidence["settings"]["demo"])
        self.assertTrue(evidence["generated_at_utc"])
        self.assertGreaterEqual(evidence["elapsed_seconds"], 0)
        self.assertIn("SYNTHETIC DEMO", report_text)
        self.assertIn("not MySQL results", report_text)
        self.assertIn("Explicit hypothetical expansion", report_text)
        self.assertNotIn("must-not-appear-in-artifacts", report_text + evidence_text + stdout.getvalue())
        self.assertFalse({"password", "host", "port", "user", "output_dir", "query_timeout"}
                         & evidence["settings"].keys())

    def test_live_configuration_is_preserved_and_sources_are_scanned_once(self):
        directory = self.output_directory() / "live"
        connection = ConfiguredConnection()
        argv = ["--output-dir", str(directory), "--host", "configured-host", "--port", "3307",
                "--user", "report-reader", "--database", "custom_demo", "--accepted-table", "loan_inputs",
                "--rejected-table", "request_inputs", "--query-timeout", "37", "--cutoffs", "600,700",
                "--baseline", "700", "--min-outcomes", "1", "--max-loss-rate", ".25",
                "--start-month", "2014-01", "--end-month", "2015-12"]
        real_dispatch = bridge.ScoreCutoffTools.call_tool
        with patch.object(bridge, "connect_with_password", return_value=connection) as connect, \
             patch.object(bridge.ScoreCutoffTools, "call_tool", autospec=True,
                          side_effect=real_dispatch) as dispatch, redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(argv), 0)
        connect.assert_called_once()
        self.assertEqual(asdict(connect.call_args.args[0]), {
            "host": "configured-host", "port": 3307, "user": "report-reader", "database": "custom_demo",
            "accepted_table": "loan_inputs", "rejected_table": "request_inputs", "query_timeout": 37,
        })
        self.assertEqual([call.args[1] for call in dispatch.call_args_list],
                         ["preview_score_cutoff_queries", "get_score_cutoff_aggregates"])
        self.assertTrue(all(set(call.args[2]) == {"cutoffs", "baseline", "start_month", "end_month"}
                            for call in dispatch.call_args_list))
        scans = [query for query, _ in connection.executed if "GROUP BY scope, bucket" in query]
        self.assertEqual(len(scans), 2)
        self.assertEqual(len(connection.transactions), 1)
        self.assertTrue(connection.transactions[0]["readonly"])
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        evidence = json.loads((directory / "aggregates.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["mode"], "historical_mysql")
        self.assertEqual(evidence["aggregates"], connection.aggregates)
        self.assertEqual(evidence["results"]["candidate_cutoff"], 600)
        self.assertEqual(evidence["results"], simulation.analyze(connection.aggregates, simulation.parse_args(argv)))
        self.assertIn("`custom_demo`.`loan_inputs`", evidence["queries"]["accepted"]["sql"])
        self.assertEqual(evidence["queries"]["accepted"]["params"],
                         [700, 700, 600, 600, "2014-01", "2015-12"])
        self.assertIn("Historical MySQL aggregate analysis", (directory / "report.md").read_text(encoding="utf-8"))

    def test_print_sql_does_not_connect_scan_or_create_output(self):
        directory = self.output_directory() / "must-not-exist"
        stdout = io.StringIO()
        real_dispatch = bridge.ScoreCutoffTools.call_tool
        with patch.object(bridge, "connect_with_password", side_effect=AssertionError("No preview database")), \
             patch.object(bridge, "getpass", side_effect=AssertionError("No preview prompt")), \
             patch.object(bridge.ScoreCutoffTools, "call_tool", autospec=True,
                          side_effect=real_dispatch) as dispatch, redirect_stdout(stdout):
            self.assertEqual(bridge.main(["--print-sql", "--output-dir", str(directory)]), 0)
        queries = json.loads(stdout.getvalue())
        self.assertEqual(set(queries), {"accepted", "rejected"})
        self.assertTrue(all("GROUP BY scope, bucket" in value["sql"] for value in queries.values()))
        self.assertEqual([call.args[1] for call in dispatch.call_args_list], ["preview_score_cutoff_queries"])
        self.assertFalse(directory.exists())

    def test_database_failure_is_sanitized_without_creating_output(self):
        directory = self.output_directory() / "must-not-exist"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(bridge, "connect_with_password",
                          side_effect=RuntimeError("borrower-secret credential-secret")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(bridge.main(["--output-dir", str(directory)]), 1)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn("failed", output.lower())
        self.assertNotIn("borrower-secret", output)
        self.assertNotIn("credential-secret", output)
        self.assertFalse(directory.exists())

    def test_paths_are_project_relative_and_original_argument_validation_is_preserved(self):
        self.assertEqual(bridge.parse_args([]).output_dir,
                         simulation.PROJECT_ROOT / "outputs" / "framework_report")
        self.assertEqual(bridge.parse_args(["--output-dir", "outputs/custom-report"]).output_dir,
                         simulation.PROJECT_ROOT / "outputs" / "custom-report")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            bridge.main(["--max-loss-rate", "nan"])
        self.assertEqual(raised.exception.code, 2)

    def test_password_factory_uses_environment_or_local_prompt(self):
        config = bridge.DatabaseConfig()
        connection = Mock()
        with patch.dict("os.environ", {"MYSQL_PASSWORD": "dummy-env-password"}), \
             patch.object(bridge, "getpass", side_effect=AssertionError("Environment must avoid prompt")), \
             patch.object(bridge.score_cutoff_sql, "connect_mysql", return_value=connection) as connect:
            self.assertIs(bridge.connect_with_password(config), connection)
        connect.assert_called_once_with(config, "dummy-env-password")
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(bridge.sys.stdin, "isatty", return_value=True), \
             patch.object(bridge, "getpass", return_value="dummy-prompt-password") as prompt, \
             patch.object(bridge.score_cutoff_sql, "connect_mysql", return_value=connection) as connect:
            self.assertIs(bridge.connect_with_password(config), connection)
        prompt.assert_called_once()
        connect.assert_called_once_with(config, "dummy-prompt-password")
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(bridge.sys.stdin, "isatty", return_value=False), \
             patch.object(bridge, "getpass", side_effect=AssertionError("No noninteractive prompt")), \
             patch.object(bridge.score_cutoff_sql, "connect_mysql") as connect:
            with self.assertRaises(bridge.ConfigurationError):
                bridge.connect_with_password(config)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
