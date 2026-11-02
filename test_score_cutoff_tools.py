"""Offline checks for the model-callable interface; no MySQL or Ollama needed."""

from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import score_cutoff_simulation as simulation
import score_cutoff_sql as database_sql
import score_cutoff_tools as tool_api
from test_score_cutoff_simulation import fixture


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False
        self.current = []

    def execute(self, query, params=()):
        self.connection.executed.append((query, params))
        if "information_schema.TABLES" in query:
            self.current = [{"ENGINE": "InnoDB"}]
        elif "information_schema.COLUMNS" in query:
            kind = params[1]
            columns = simulation.REQUIRED[kind] - self.connection.missing.get(kind, set())
            self.current = [{"COLUMN_NAME": field} for field in sorted(columns)]
        elif "GROUP BY scope, bucket" in query:
            if self.connection.failure is not None:
                raise self.connection.failure
            kind = "accepted" if "`accepted`" in query else "rejected"
            self.current = self.connection.aggregates[kind]
        else:
            raise AssertionError(f"Unexpected query: {query}")

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return self.current

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, *, missing=None, failure=None):
        self.aggregates = fixture()
        self.missing = missing or {}
        self.failure = failure
        self.executed = []
        self.transactions = []
        self.cursors = []
        self.rollbacks = 0
        self.closed = False

    def cursor(self, dictionary=False):
        if not dictionary:
            raise AssertionError("Expected dictionary cursor")
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def start_transaction(self, **settings):
        self.transactions.append(settings)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class ScoreCutoffToolTests(unittest.TestCase):
    @staticmethod
    def connector_modules(connection):
        mysql = ModuleType("mysql")
        connector = ModuleType("mysql.connector")
        connector.connect = Mock(return_value=connection)
        mysql.connector = connector
        return {"mysql": mysql, "mysql.connector": connector}

    def test_sql_connection_uses_host_settings_and_parameterized_timeout(self):
        connection = Mock()
        modules = self.connector_modules(connection)
        config = tool_api.DatabaseConfig(host="test-host", port=3307, user="report-reader",
                                         query_timeout=37)
        with patch.dict(sys.modules, modules):
            result = database_sql.connect_mysql(config, "dummy-test-password")
        self.assertIs(result, connection)
        modules["mysql.connector"].connect.assert_called_once_with(
            host="test-host", port=3307, user="report-reader", password="dummy-test-password",
            charset="utf8mb4", connection_timeout=10, autocommit=True, sql_mode="TRADITIONAL",
        )
        connection.cursor.return_value.execute.assert_called_once_with(
            "SET SESSION MAX_EXECUTION_TIME = %s", (37000,),
        )
        connection.cursor.return_value.close.assert_called_once_with()
        connection.close.assert_not_called()

    def test_sql_connection_is_closed_when_timeout_configuration_fails(self):
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = RuntimeError("timeout setup failed")
        with patch.dict(sys.modules, self.connector_modules(connection)):
            with self.assertRaisesRegex(RuntimeError, "timeout setup failed"):
                database_sql.connect_mysql(tool_api.DatabaseConfig(), "dummy-test-password")
        connection.cursor.return_value.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_sql_builder_rejects_unknown_sources(self):
        args = simulation.parse_args([])
        for kind in ("unknown", "accepted; SELECT 1", "ACCEPTED", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                database_sql.build_query(kind, args)

    def test_dictionary_is_a_complete_independent_function_catalog(self):
        catalog = tool_api.load_tool_dictionary()
        definitions = tool_api.get_tool_definitions()
        self.assertEqual(catalog["tools"], definitions)
        names = {item["function"]["name"] for item in definitions}
        self.assertEqual(names, {
            "inspect_sources", "preview_score_cutoff_queries",
            "get_score_cutoff_aggregates", "compare_score_cutoffs",
        })
        self.assertEqual(set(catalog["tool_details"]), names)
        runner = tool_api.ScoreCutoffTools(connection_factory=lambda config: FakeConnection())
        for item in definitions:
            with self.subTest(tool=item["function"]["name"]):
                self.assertEqual(item["type"], "function")
                function = item["function"]
                self.assertTrue(callable(getattr(runner, function["name"])))
                self.assertTrue(function["description"])
                self.assertEqual(function["parameters"]["type"], "object")
                self.assertFalse(function["parameters"]["additionalProperties"])
                self.assertFalse({"sql", "password", "host", "output_dir", "database"}
                                 & function["parameters"]["properties"].keys())
        definitions[0]["function"]["name"] = "mutated"
        catalog["tools"].clear()
        self.assertEqual(len(tool_api.load_tool_dictionary()["tools"]), 4)
        self.assertNotEqual(tool_api.get_tool_definitions()[0]["function"]["name"], "mutated")

    def test_dictionary_examples_execute_with_documented_arguments(self):
        catalog = tool_api.load_tool_dictionary()
        for name, details in catalog["tool_details"].items():
            examples = [details["example"]]
            if "hypothetical_example" in details:
                examples.append(details["hypothetical_example"])
            for example in examples:
                with self.subTest(tool=name, arguments=example["arguments"]):
                    connection = FakeConnection()
                    runner = tool_api.ScoreCutoffTools(
                        connection_factory=lambda config: connection,
                    )
                    self.assertEqual(example["name"], name)
                    response = runner.call_tool(example["name"], example["arguments"])
                    self.assertTrue(response["ok"], response)

    def test_database_comparison_matches_existing_report_calculations(self):
        request = {
            "cutoffs": [740, 600, 740], "baseline": 680, "min_outcomes": 1,
            "max_loss_rate": .08, "assume_comparable_scores": True,
            "rejected_loss_rate": .15, "take_up_rate": .25,
        }
        factory = Mock(return_value=FakeConnection())
        runner = tool_api.ScoreCutoffTools(connection_factory=factory)
        response = runner.call_tool("compare_score_cutoffs", json.dumps(request))
        self.assertTrue(response["ok"], response)
        result = response["result"]
        expected_args = simulation.parse_args([
            "--cutoffs", "740,600,740", "--baseline", "680",
            "--min-outcomes", "1", "--max-loss-rate", ".08",
            "--assume-comparable-scores", "--rejected-loss-rate", ".15",
            "--take-up-rate", ".25",
        ])
        expected = simulation.analyze(fixture(), expected_args)
        self.assertEqual(result["results"], expected)
        self.assertEqual(result["settings"]["cutoffs"], [600, 680, 740])
        self.assertEqual(result["mode"], "historical_mysql")
        self.assertTrue(result["limitations"])
        json.dumps(response, allow_nan=False)
        factory.assert_called_once_with(runner.config)

    def test_database_aggregates_are_disjoint_fixtures_not_cumulative_results(self):
        runner = tool_api.ScoreCutoffTools(connection_factory=lambda config: FakeConnection())
        result = runner.get_score_cutoff_aggregates(cutoffs=[600, 700], baseline=700)
        self.assertEqual(result["aggregates"], fixture())
        self.assertNotIn("results", result)
        self.assertEqual(result["mode"], "historical_mysql")

    def test_query_preview_needs_no_database_and_preserves_parameters(self):
        factory = Mock(side_effect=AssertionError("Preview must not connect"))
        config = tool_api.DatabaseConfig(database="dem`o", accepted_table="acc`epted")
        runner = tool_api.ScoreCutoffTools(config, connection_factory=factory)
        response = runner.call_tool("preview_score_cutoff_queries", {
            "cutoffs": [600, 700], "baseline": 700,
            "start_month": "2014-01", "end_month": "2015-12",
        })
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["mode"], "query_preview")
        args = simulation.parse_args([
            "--cutoffs", "600,700", "--baseline", "700", "--database", "dem`o",
            "--accepted-table", "acc`epted", "--start-month", "2014-01",
            "--end-month", "2015-12",
        ])
        for kind, query in response["result"]["queries"].items():
            sql, params = simulation.build_query(kind, args)
            self.assertEqual(query["sql"], sql)
            self.assertEqual(query["params"], list(params))
            self.assertNotIn("2014-01", query["sql"])
        factory.assert_not_called()

    def test_invalid_model_arguments_fail_before_connection(self):
        factory = Mock(side_effect=AssertionError("Invalid request reached the database"))
        runner = tool_api.ScoreCutoffTools(connection_factory=factory)
        invalid = [
            {"cutoffs": "600,700"}, {"cutoffs": []}, {"cutoffs": None},
            {"cutoffs": [True]}, {"cutoffs": [700.0]}, {"cutoffs": [299]},
            {"cutoffs": [851]}, {"cutoffs": list(range(600, 630)), "baseline": 700},
            {"baseline": True}, {"baseline": "660"}, {"min_outcomes": 0},
            {"min_outcomes": True}, {"max_loss_rate": True},
            {"max_loss_rate": float("nan")}, {"max_loss_rate": float("inf")},
            {"max_loss_rate": -0.1}, {"max_loss_rate": 1.1},
            {"start_month": "2016-13"}, {"start_month": "2016-1"},
            {"start_month": "2016-01\n"}, {"end_month": 2016},
            {"start_month": "2017-01", "end_month": "2016-12"},
            {"assume_comparable_scores": "true"}, {"assume_comparable_scores": True},
            {"rejected_loss_rate": .1}, {"take_up_rate": .5},
            {"assume_comparable_scores": True, "rejected_loss_rate": -1},
            {"host": "another-server"}, {"password": "must-not-echo"},
            {"sql": "SELECT * FROM accepted"}, {"output_dir": "../outside"},
            {"demo": True}, [], None, "[]", "null", "{", '{"baseline": 660, "baseline": 700}',
            '{"max_loss_rate": NaN}', '{"max_loss_rate": Infinity}',
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                response = runner.call_tool("compare_score_cutoffs", arguments)
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "invalid_arguments")
                self.assertNotIn("must-not-echo", json.dumps(response))
        factory.assert_not_called()

    def test_unknown_tools_and_direct_methods_cannot_bypass_validation(self):
        factory = Mock(side_effect=AssertionError("Invalid request reached the database"))
        runner = tool_api.ScoreCutoffTools(connection_factory=factory)
        for name in ("execute_sql", "__class__", "_connect", None, ["inspect_sources"]):
            response = runner.call_tool(name, {})
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "unknown_tool")
        with self.assertRaises(ValueError):
            runner.compare_score_cutoffs(cutoffs=[True])
        with self.assertRaises(ValueError):
            runner.inspect_sources(sql="SELECT * FROM accepted")
        factory.assert_not_called()

    def test_live_comparison_uses_one_read_only_snapshot_and_closes_connection(self):
        connection = FakeConnection()
        factory = Mock(return_value=connection)
        runner = tool_api.ScoreCutoffTools(connection_factory=factory)
        response = runner.call_tool("compare_score_cutoffs", {
            "cutoffs": [600, 700], "baseline": 700, "min_outcomes": 1,
            "max_loss_rate": .25,
        })
        self.assertTrue(response["ok"], response)
        result = response["result"]
        self.assertEqual(result["mode"], "historical_mysql")
        self.assertEqual(result["results"]["candidate_cutoff"], 600)
        self.assertAlmostEqual(result["results"]["scenarios"][0]["loss_rate"], .2)
        factory.assert_called_once_with(runner.config)
        self.assertEqual(connection.transactions, [{
            "isolation_level": "REPEATABLE READ", "readonly": True, "consistent_snapshot": True,
        }])
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))
        self.assertEqual(len(connection.executed), 6)
        self.assertTrue(all("information_schema" in sql for sql, _ in connection.executed[:4]))
        self.assertTrue(all("GROUP BY scope, bucket" in sql for sql, _ in connection.executed[4:]))

    def test_live_aggregate_decimal_results_are_json_safe(self):
        connection = FakeConnection()
        connection.aggregates["accepted"][0]["amount_sum"] = Decimal("300.0000")
        connection.aggregates["accepted"][0]["performance_rows"] = Decimal("2")
        runner = tool_api.ScoreCutoffTools(connection_factory=lambda config: connection)
        response = runner.call_tool("get_score_cutoff_aggregates", {"cutoffs": [600, 700], "baseline": 700})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["aggregates"], fixture())
        json.dumps(response, allow_nan=False)
        self.assertTrue(connection.closed)

    def test_schema_inspection_does_not_scan_sources(self):
        connection = FakeConnection(missing={"rejected": {"Risk_Score"}})
        runner = tool_api.ScoreCutoffTools(connection_factory=lambda config: connection)
        response = runner.call_tool("inspect_sources", {})
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["result"]["schema_checked"])
        sources = response["result"]["sources"]
        self.assertTrue(sources["accepted"]["valid"])
        self.assertFalse(sources["rejected"]["valid"])
        self.assertEqual(sources["rejected"]["missing_columns"], ["Risk_Score"])
        self.assertTrue(all("information_schema" in sql for sql, _ in connection.executed))
        self.assertTrue(connection.closed)
        self.assertEqual(connection.rollbacks, 1)

    def test_incompatible_schema_blocks_both_data_scans(self):
        connection = FakeConnection(missing={"rejected": {"Risk_Score"}})
        runner = tool_api.ScoreCutoffTools(connection_factory=lambda config: connection)
        response = runner.call_tool("get_score_cutoff_aggregates", {})
        self.assertFalse(response["ok"])
        self.assertTrue(all("information_schema" in sql for sql, _ in connection.executed))
        self.assertTrue(connection.closed)
        self.assertEqual(connection.rollbacks, 1)

    def test_failures_are_sanitized_and_release_database_resources(self):
        class ConnectorFailure(Exception):
            __module__ = "mysql.connector.errors"

        connection = FakeConnection(failure=ConnectorFailure("source-row-secret credential-secret"))
        runner = tool_api.ScoreCutoffTools(connection_factory=lambda config: connection)
        response = runner.call_tool("compare_score_cutoffs", {})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "database_error")
        self.assertNotIn("source-row-secret", json.dumps(response))
        self.assertNotIn("credential-secret", json.dumps(response))
        self.assertTrue(connection.closed)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))

    def test_live_calls_require_local_credentials_without_prompting(self):
        with patch.dict("os.environ", {}, clear=True):
            response = tool_api.ScoreCutoffTools().call_tool("inspect_sources", {})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "configuration_error")

    def test_cli_outputs_only_machine_readable_json(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(tool_api.ScoreCutoffTools, "_connect", return_value=FakeConnection()):
            code = tool_api.main([ "--call", "compare_score_cutoffs"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        output = io.StringIO()
        with redirect_stdout(output), patch.object(tool_api.ScoreCutoffTools, "_connect", return_value=FakeConnection()):
            code = tool_api.main([ "--call", "compare_score_cutoffs",
                                  "--arguments", '{"sql":"SELECT 1"}'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
