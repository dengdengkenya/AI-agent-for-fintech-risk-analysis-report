"""Offline checks for aggregate-only cutoff simulations; no MySQL writes needed."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import score_cutoff_simulation as simulation


def accepted(bucket, rows, amount_rows, amount, terminal=0, bad=0,
             complete=0, principal=0, loss=0, scope="in_scope"):
    return {"scope": scope, "bucket": bucket, "rows": rows,
            "amount_rows": amount_rows, "amount_sum": amount,
            "terminal_rows": terminal, "charged_off_rows": bad,
            "performance_rows": complete, "performance_principal": principal,
            "loss_amount": loss}


def fixture():
    return {
        "accepted": [
            accepted(600, 3, 3, 300, terminal=2, bad=1, complete=2,
                     principal=200, loss=80),
            accepted(700, 2, 2, 200, terminal=2, complete=2, principal=200),
            accepted(0, 5, 5, 500),
            accepted(-1, 11, 10, 1000),
            accepted(700, 7, 7, 700, scope="invalid_date"),
            accepted(700, 6, 6, 600, scope="outside_period"),
        ],
        "rejected": [
            {"scope": "in_scope", "bucket": 600, "rows": 4,
             "amount_rows": 3, "amount_sum": 600},
            {"scope": "in_scope", "bucket": 700, "rows": 1,
             "amount_rows": 1, "amount_sum": 400},
        ],
    }


class FakeCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.executed = []
        self.closed = False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self.current = next(self.responses)

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return self.current

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, responses):
        self.fake_cursor = FakeCursor(responses)
        self.transaction = None
        self.rollbacks = 0

    def start_transaction(self, **kwargs):
        self.transaction = kwargs

    def cursor(self, *, dictionary):
        if not dictionary:
            raise AssertionError("Expected aggregate dictionaries")
        return self.fake_cursor

    def rollback(self):
        self.rollbacks += 1


class ScoreCutoffSimulationTests(unittest.TestCase):
    def args(self, *extra):
        return simulation.parse_args([
            "--cutoffs", "600,700", "--baseline", "700", "--min-outcomes", "1", *extra
        ])

    def test_cumulative_cutoffs_and_weighted_loss_denominator(self):
        result = simulation.analyze(fixture(), self.args())
        lower, baseline = result["scenarios"]
        self.assertEqual(lower["accepted"]["rows"], 5)
        self.assertEqual(lower["accepted"]["amount_sum"], 500)
        self.assertEqual(lower["accepted_count_change"], 3)
        self.assertEqual(lower["accepted_volume_change"], 300)
        self.assertAlmostEqual(lower["accepted_growth_rate"], 1.5)
        self.assertAlmostEqual(lower["accepted_retention_rate"], .5)
        self.assertAlmostEqual(lower["terminal_share"], .8)
        self.assertAlmostEqual(lower["chargeoff_rate"], .25)
        self.assertAlmostEqual(lower["loss_rate"], .2)
        self.assertAlmostEqual(lower["loss_rate_change_pp"], 20)
        self.assertEqual(baseline["accepted_count_change"], 0)
        self.assertEqual(baseline["loss_rate"], 0)
        self.assertEqual(baseline["loss_rate_change_pp"], 0)

    def test_exclusions_reconcile_without_becoming_low_score_zeroes(self):
        result = simulation.analyze(fixture(), self.args())
        quality = result["quality"]["accepted"]
        self.assertEqual(quality["total_rows"], 34)
        self.assertEqual(quality["in_scope_rows"], 21)
        self.assertEqual(quality["invalid_date_rows"], 7)
        self.assertEqual(quality["outside_period_rows"], 6)
        self.assertEqual(quality["invalid_score_rows"], 11)
        self.assertEqual(quality["invalid_amount_rows"], 1)

    def test_rejects_do_not_silently_become_approvals_or_losses(self):
        row = simulation.analyze(fixture(), self.args())["scenarios"][0]
        self.assertEqual(row["rejected"]["rows"], 5)
        self.assertEqual(row["rejected"]["amount_rows"], 4)
        self.assertIsNone(row["assumed_new_loans"])
        self.assertIsNone(row["assumed_new_principal"])
        self.assertIsNone(row["assumed_new_loss"])
        self.assertNotIn("hypothetical_total_loans", row)

    def test_expansion_uses_only_valid_amounts_and_explicit_assumptions(self):
        args = self.args("--assume-comparable-scores", "--rejected-loss-rate", ".2",
                         "--take-up-rate", ".25")
        lower, baseline = simulation.analyze(fixture(), args)["scenarios"]
        self.assertEqual(lower["assumed_new_loans"], 1)
        self.assertEqual(lower["assumed_new_principal"], 250)
        self.assertEqual(lower["assumed_new_loss"], 50)
        self.assertEqual(lower["hypothetical_total_loans"], 6)
        self.assertEqual(lower["hypothetical_count_change"], 4)
        # Expansion is measured against the accepted-only baseline, even here.
        self.assertEqual(baseline["hypothetical_count_change"], .25)
        self.assertEqual(lower["loss_rate"], .2)

    def test_empty_and_sparse_outcomes_are_unavailable_not_zero_loss(self):
        empty = simulation.analyze({"accepted": [], "rejected": []}, self.args())
        for row in empty["scenarios"]:
            for field in ("loss_rate", "chargeoff_rate", "accepted_growth_rate",
                          "terminal_share", "loss_rate_change_pp", "loss_data_coverage"):
                self.assertIsNone(row[field], field)
        sparse = simulation.analyze(fixture(), self.args("--min-outcomes", "5"))
        self.assertTrue(all(row["loss_rate"] is None for row in sparse["scenarios"]))
        self.assertTrue(all(row["chargeoff_rate"] is None for row in sparse["scenarios"]))
        self.assertIn("N/A", simulation.render_report(sparse, self.args("--min-outcomes", "5")))

    def test_candidate_requires_ceiling_and_complete_financial_coverage(self):
        self.assertIsNone(simulation.analyze(fixture(), self.args())["candidate_cutoff"])
        self.assertEqual(simulation.analyze(
            fixture(), self.args("--max-loss-rate", ".1"))["candidate_cutoff"], 700)
        self.assertEqual(simulation.analyze(
            fixture(), self.args("--max-loss-rate", ".25"))["candidate_cutoff"], 600)
        incomplete = fixture()
        incomplete["accepted"][1]["terminal_rows"] = 3
        self.assertIsNone(simulation.analyze(
            incomplete, self.args("--max-loss-rate", ".25"))["candidate_cutoff"])

    def test_cutoffs_are_sorted_unique_and_include_baseline(self):
        args = simulation.parse_args(["--cutoffs", "740,600,740", "--baseline", "680"])
        self.assertEqual(args.cutoffs, [600, 680, 740])
        self.assertEqual([row["cutoff"] for row in simulation.analyze(
            simulation.demo_aggregates(args), args)["scenarios"]], [600, 680, 740])

    def test_invalid_arguments_fail_before_connecting(self):
        invalid = [
            ["--cutoffs", "299,700"], ["--cutoffs", "600,"], ["--baseline", "851"],
            ["--start-month", "2016-13"],
            ["--start-month", "2017-01", "--end-month", "2016-12"],
            ["--rejected-loss-rate", ".1"], ["--assume-comparable-scores"],
            ["--take-up-rate", ".5"], ["--max-loss-rate", "nan"],
            ["--max-loss-rate", "inf"], ["--max-loss-rate", "1.01"],
            ["--max-loss-rate", "-.1"], ["--min-outcomes", "0"],
            ["--port", "0"], ["--query-timeout", "0"],
            ["--accepted-table", "same", "--rejected-table", "SAME"],
            ["--demo", "--start-month", "2016-01"], ["--database", "bad\x00name"],
        ]
        for argv in invalid:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    simulation.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_queries_are_grouped_parameterized_and_quote_identifiers(self):
        args = self.args("--database", "dem`o", "--accepted-table", "acc`epted",
                         "--start-month", "2014-01", "--end-month", "2015-12")
        for kind in ("accepted", "rejected"):
            sql, params = simulation.build_query(kind, args)
            self.assertTrue(sql.lstrip().startswith("WITH parsed AS"))
            self.assertIn("GROUP BY scope, bucket", sql)
            self.assertIn("COUNT(*)", sql)
            self.assertEqual(sql.count("%s"), len(params))
            self.assertEqual(params, (700, 700, 600, 600, "2014-01", "2015-12"))
            self.assertNotIn("2014-01", sql)
            self.assertNotRegex(sql.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE)\b")
            for sensitive_column in ("`id`", "`member_id`", "`desc`", "`url`", "`zip_code`"):
                self.assertNotIn(sensitive_column, sql)
        accepted_sql, _ = simulation.build_query("accepted", args)
        self.assertIn("`dem``o`.`acc``epted`", accepted_sql)
        self.assertIn("CASE WHEN TRIM(`funded_amnt`) REGEXP", accepted_sql)
        self.assertIn("GREATEST(amount-principal_received-recovered,0)", accepted_sql)

    def test_read_only_snapshot_schema_checks_and_rollback(self):
        aggregates = fixture()
        responses = []
        for kind in ("accepted", "rejected"):
            responses += [[{"ENGINE": "InnoDB"}],
                          [{"COLUMN_NAME": name} for name in simulation.REQUIRED[kind]]]
        responses += [aggregates["accepted"], aggregates["rejected"]]
        connection = FakeConnection(responses)
        with redirect_stdout(io.StringIO()):
            result = simulation.fetch_aggregates(connection, self.args())
        self.assertEqual(result, aggregates)
        self.assertEqual(connection.transaction, {
            "isolation_level": "REPEATABLE READ", "readonly": True, "consistent_snapshot": True})
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.fake_cursor.closed)
        executed = connection.fake_cursor.executed
        self.assertEqual(len(executed), 6)
        self.assertTrue(all("information_schema" in sql for sql, _ in executed[:4]))
        self.assertTrue(all("GROUP BY scope, bucket" in sql for sql, _ in executed[4:]))

    def test_missing_schema_fails_before_aggregate_queries_and_rolls_back(self):
        connection = FakeConnection([[{"ENGINE": "InnoDB"}], []])
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            simulation.fetch_aggregates(connection, self.args())
        self.assertEqual(len(connection.fake_cursor.executed), 2)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.fake_cursor.closed)

    def test_demo_writes_labeled_report_without_credentials_or_mysql(self):
        output_root = simulation.PROJECT_ROOT / "outputs"
        output_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cutoff-test-", dir=output_root) as temp:
            folder = Path(temp).resolve()
            self.assertTrue(folder.is_relative_to(output_root.resolve()))
            with patch.dict("os.environ", {"MYSQL_PASSWORD": "do-not-export-this"}), \
                 patch.object(simulation, "getpass", side_effect=AssertionError("Unexpected password prompt")), \
                 patch.object(simulation, "fetch_aggregates", side_effect=AssertionError("Unexpected MySQL read")), \
                 redirect_stdout(io.StringIO()):
                simulation.main(["--demo", "--output-dir", str(folder)])
            report = (folder / "report.md").read_text(encoding="utf-8")
            evidence_text = (folder / "aggregates.json").read_text(encoding="utf-8")
            evidence = json.loads(evidence_text)
            self.assertIn("SYNTHETIC DEMO", report)
            self.assertIn("not MySQL results", report)
            self.assertIn("growth versus principal losses", report)
            self.assertNotIn("do-not-export-this", report + evidence_text)
            self.assertTrue(evidence["settings"]["demo"])
            self.assertEqual(evidence["queries"], {})
            self.assertEqual(len(evidence["results"]["scenarios"]), 7)
            self.assertIsNone(evidence["results"]["candidate_cutoff"])

    def test_print_sql_needs_no_credentials_or_output_files(self):
        output = io.StringIO()
        with redirect_stdout(output), \
             patch.object(simulation, "getpass", side_effect=AssertionError("Unexpected prompt")), \
             patch.object(simulation, "fetch_aggregates", side_effect=AssertionError("Unexpected MySQL read")):
            simulation.main(["--print-sql"])
        queries = json.loads(output.getvalue())
        self.assertEqual(set(queries), {"accepted", "rejected"})
        self.assertTrue(all("GROUP BY" in query["sql"] for query in queries.values()))


if __name__ == "__main__":
    unittest.main()
