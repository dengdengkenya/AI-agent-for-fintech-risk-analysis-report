"""MySQL boundary and aggregate cube checks; no running database is needed."""

import json
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from loan_analyst import db
from loan_analyst.cube import (ACCEPTED, DIMENSIONS, REJECTED, REQUIRED, SchemaError, Snapshot, build_query,
                               load_snapshot, read_snapshot, save_snapshot)
from loan_analyst.settings import DatabaseConfig
from tests.fixtures import FakeConnection, cube_rows, make_snapshot, temporary_directory


def connector_modules(connection):
    mysql = ModuleType("mysql")
    connector = ModuleType("mysql.connector")
    connector.connect = Mock(return_value=connection)
    mysql.connector = connector
    return {"mysql": mysql, "mysql.connector": connector}


class DatabaseTests(unittest.TestCase):
    def test_connection_uses_host_settings_and_parameterized_timeout(self):
        connection = Mock()
        modules = connector_modules(connection)
        config = DatabaseConfig(host="test-host", port=3307, user="report-reader", query_timeout=37)
        with patch.dict(sys.modules, modules):
            self.assertIs(db.connect_mysql(config, "dummy-test-password"), connection)
        modules["mysql.connector"].connect.assert_called_once_with(
            host="test-host", port=3307, user="report-reader", password="dummy-test-password",
            charset="utf8mb4", connection_timeout=10, autocommit=True, sql_mode="TRADITIONAL")
        connection.cursor.return_value.execute.assert_called_once_with(
            "SET SESSION MAX_EXECUTION_TIME = %s", (37000,))
        connection.close.assert_not_called()

    def test_connection_is_closed_when_timeout_configuration_fails(self):
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = RuntimeError("timeout setup failed")
        with patch.dict(sys.modules, connector_modules(connection)), self.assertRaises(RuntimeError):
            db.connect_mysql(DatabaseConfig(), "dummy-test-password")
        connection.close.assert_called_once_with()

    def test_password_comes_from_environment_or_local_prompt_only(self):
        config, connection = DatabaseConfig(), Mock()
        with patch.dict("os.environ", {"MYSQL_PASSWORD": "dummy-env-password"}), \
             patch.object(db, "getpass", side_effect=AssertionError("Environment must avoid prompt")), \
             patch.object(db, "connect_mysql", return_value=connection) as connect:
            self.assertIs(db.password_connection_factory(config), connection)
        connect.assert_called_once_with(config, "dummy-env-password")
        with patch.dict("os.environ", {}, clear=True), patch.object(db.sys.stdin, "isatty", return_value=True), \
             patch.object(db, "getpass", return_value="dummy-prompt-password"), \
             patch.object(db, "connect_mysql", return_value=connection) as connect:
            db.password_connection_factory(config)
        connect.assert_called_once_with(config, "dummy-prompt-password")
        with patch.dict("os.environ", {}, clear=True), patch.object(db.sys.stdin, "isatty", return_value=False), \
             patch.object(db, "connect_mysql") as connect, self.assertRaises(db.ConfigurationError):
            db.password_connection_factory(config)
        connect.assert_not_called()


class QueryTests(unittest.TestCase):
    def test_queries_are_fixed_grouped_and_quote_identifiers(self):
        config = DatabaseConfig(database="dem`o", accepted_table="acc`epted")
        for kind in (ACCEPTED, REJECTED):
            sql = build_query(kind, config)
            self.assertTrue(sql.lstrip().startswith("WITH parsed AS"))
            self.assertIn("GROUP BY period, score", sql)
            self.assertIn("COUNT(*)", sql)
            self.assertNotIn("%s", sql)
            self.assertNotRegex(sql.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE|ALTER)\b")
            for sensitive in ("`id`", "`member_id`", "`desc`", "`url`", "`zip_code`", "`emp_title`", "`title`"):
                self.assertNotIn(sensitive, sql)
        accepted = build_query(ACCEPTED, config)
        self.assertIn("`dem``o`.`acc``epted`", accepted)
        self.assertIn("CASE WHEN TRIM(`funded_amnt`) REGEXP", accepted)
        self.assertIn("GREATEST(amount-principal_received-recovered,0)", accepted)
        self.assertIn("GROUP BY period, score, `grade`, `term`", accepted)
        self.assertIn("GROUP BY period, score ORDER BY", build_query(ACCEPTED, config, dimensions=()))

    def test_unknown_sources_are_rejected(self):
        for kind in ("unknown", "accepted; SELECT 1", "ACCEPTED", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                build_query(kind, DatabaseConfig())


class SnapshotReadTests(unittest.TestCase):
    def test_both_schemas_are_checked_then_each_table_is_scanned_once_read_only(self):
        connection = FakeConnection()
        snapshot = read_snapshot(connection, DatabaseConfig(), clock=lambda: "2026-11-03T10:00:00+00:00")
        self.assertEqual(connection.transactions, [
            {"isolation_level": "REPEATABLE READ", "readonly": True, "consistent_snapshot": True}])
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))
        queries = [query for query, _ in connection.executed]
        self.assertEqual(len(queries), 6)
        self.assertTrue(all("information_schema" in query for query in queries[:4]))
        self.assertTrue(all("GROUP BY period, score" in query for query in queries[4:]))
        self.assertEqual(snapshot.cubes[ACCEPTED].rows, cube_rows()[ACCEPTED])
        self.assertEqual(snapshot.cubes[REJECTED].rows, cube_rows()[REJECTED])
        row = snapshot.cubes[ACCEPTED].rows[0]
        self.assertIs(type(row["rows"]), int)
        self.assertIs(type(row["amount_sum"]), float)
        self.assertIs(type(row["score"]), float)
        self.assertEqual(snapshot.sources[ACCEPTED]["dimensions"], ["grade", "term"])
        json.dumps(snapshot.to_payload(), allow_nan=False)

    def test_optional_segment_columns_are_used_only_when_present(self):
        connection = FakeConnection(missing={ACCEPTED: {"grade"}})
        snapshot = read_snapshot(connection, DatabaseConfig())
        self.assertEqual(snapshot.sources[ACCEPTED]["dimensions"], ["term"])
        scan = next(query for query, _ in connection.executed if "`accepted`" in query and "GROUP BY" in query)
        self.assertNotIn("`grade`", scan)
        self.assertNotIn("grade", snapshot.cubes[ACCEPTED].rows[0])

    def test_invalid_sources_fail_before_any_scan_and_roll_back(self):
        for connection in (FakeConnection(missing={REJECTED: {"Risk_Score"}}), FakeConnection(engine="MyISAM")):
            with self.subTest(engine=connection.engine), self.assertRaises(SchemaError):
                read_snapshot(connection, DatabaseConfig())
            self.assertTrue(all("information_schema" in query for query, _ in connection.executed))
            self.assertEqual(connection.rollbacks, 1)
        empty = FakeConnection(rows={ACCEPTED: [], REJECTED: cube_rows()[REJECTED]})
        with self.assertRaisesRegex(SchemaError, "empty"):
            read_snapshot(empty, DatabaseConfig())

    def test_required_columns_are_unchanged(self):
        self.assertEqual(REQUIRED[ACCEPTED], {"fico_range_low", "fico_range_high", "funded_amnt", "issue_d",
                                              "loan_status", "total_rec_prncp", "recoveries"})
        self.assertEqual(REQUIRED[REJECTED], {"Risk_Score", "Amount Requested", "Application Date"})
        self.assertEqual([item.name for item in DIMENSIONS[ACCEPTED]], ["grade", "term"])


class CubeTests(unittest.TestCase):
    def test_bands_reproduce_the_reviewed_grouped_aggregates(self):
        bands = make_snapshot().cubes[ACCEPTED].bands((600, 700), "2013-01")
        simplified = [{key: band[key] for key in ("scope", "bucket", "rows", "amount_rows", "amount_sum",
                                                  "terminal_rows", "loss_amount")} for band in bands]
        self.assertEqual(simplified, [
            {"scope": "in_scope", "bucket": -1, "rows": 11, "amount_rows": 10, "amount_sum": 1000,
             "terminal_rows": 0, "loss_amount": 0},
            {"scope": "in_scope", "bucket": 0, "rows": 5, "amount_rows": 5, "amount_sum": 500,
             "terminal_rows": 0, "loss_amount": 0},
            {"scope": "in_scope", "bucket": 600, "rows": 3, "amount_rows": 3, "amount_sum": 300,
             "terminal_rows": 2, "loss_amount": 80},
            {"scope": "in_scope", "bucket": 700, "rows": 2, "amount_rows": 2, "amount_sum": 200,
             "terminal_rows": 2, "loss_amount": 0},
            {"scope": "invalid_date", "bucket": 700, "rows": 7, "amount_rows": 7, "amount_sum": 700,
             "terminal_rows": 0, "loss_amount": 0},
            {"scope": "outside_period", "bucket": 700, "rows": 6, "amount_rows": 6, "amount_sum": 600,
             "terminal_rows": 0, "loss_amount": 0},
        ])
        self.assertEqual((bands[2]["first_month"], bands[2]["last_month"]), ("2015-03", "2015-03"))

    def test_snapshots_round_trip_with_integrity_checks(self):
        folder = temporary_directory(self, "snapshot-test-")
        snapshot = make_snapshot()
        path = save_snapshot(snapshot, folder)
        self.assertEqual(path.name, f"snapshot-{snapshot.snapshot_id}.json")
        self.assertRegex(snapshot.snapshot_id, r"^20261103T100400Z-[0-9a-f]{8}$")
        loaded = load_snapshot(str(path), folder, DatabaseConfig())
        self.assertEqual(loaded.content_sha256, snapshot.content_sha256)
        self.assertEqual(loaded.cubes[ACCEPTED].rows, snapshot.cubes[ACCEPTED].rows)
        self.assertIn("saved file", loaded.origin)
        self.assertEqual(load_snapshot("latest", folder, DatabaseConfig()).snapshot_id, snapshot.snapshot_id)
        with self.assertRaisesRegex(ValueError, "other tables"):
            load_snapshot(str(path), folder, DatabaseConfig(accepted_table="loans"))
        with self.assertRaisesRegex(ValueError, "No saved snapshot"):
            load_snapshot("latest", folder, DatabaseConfig(database="other"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cubes"][ACCEPTED]["rows"][0][payload["cubes"][ACCEPTED]["columns"].index("rows")] += 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash"):
            load_snapshot(str(path), folder, DatabaseConfig())
        path.write_text('{"format": "something else"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Not a complete"):
            Snapshot.from_payload(json.loads(path.read_text(encoding="utf-8")), "test")


if __name__ == "__main__":
    unittest.main()
