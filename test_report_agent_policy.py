"""Policy report checks: python -m unittest test_report_agent_policy -v."""

import csv
import gzip
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from report_agent import lendingclub
from report_agent.policy import (BadRateModel, Scenario, _simulate, applicant_drift, approval_funnel,
                                 clean_scenario, cutoff_simulation, scenario_from_text)
from report_agent.synthetic_data import build_database


class ScenarioTests(unittest.TestCase):
    def test_text_fallback(self):
        s = scenario_from_text("如果把评分线放宽到580分，DTI上限放到50%会怎样")
        self.assertEqual((s.score_cutoff, s.max_dti), (580, 50))
        self.assertTrue(scenario_from_text("常规月度报告").is_empty())

    def test_model_json_is_range_checked(self):
        s = clean_scenario({"score_cutoff": "620", "max_dti": 400})
        self.assertEqual((s.score_cutoff, s.max_dti), (620, None))
        self.assertTrue(clean_scenario(None).is_empty())


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "demo.db"
        build_database(cls.db)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def apps(self):
        return pd.read_sql_query("SELECT * FROM applications", self.conn)

    def test_unchanged_policy_changes_nothing(self):
        df = self.apps()
        model = BadRateModel(df[(df.approved == 1) & df.bad.notna()])
        groups, _ = _simulate(df, model, 600, 45, 600, 45)
        self.assertEqual(len(groups["swap_in"]) + len(groups["swap_out"]), 0)
        self.assertEqual(len(groups["keep"]), int(df.approved.sum()))

    def test_groups_partition_applicants(self):
        df = self.apps()
        model = BadRateModel(df[(df.approved == 1) & df.bad.notna()])
        for new_cut, new_dti in [(620, 45), (580, 50), (600, 35)]:
            groups, rates = _simulate(df, model, 600, 45, new_cut, new_dti)
            self.assertEqual(sum(len(g) for g in groups.values()), len(df))
            if len(groups["swap_out"]):
                self.assertGreater(rates["swap_out"], rates["keep"])

    def test_tighten_lowers_and_loosen_raises_bad_rate(self):
        tight = cutoff_simulation(self.conn, Scenario(620, None))
        loose = cutoff_simulation(self.conn, Scenario(580, 50))
        self.assertIn("减少", tight.facts[1])
        self.assertIn("增加", loose.facts[1])
        self.assertTrue(any("估算" in f for f in loose.facts))
        self.assertFalse(any("估算" in v for v in tight.table["新策略"]))

    def test_other_reports_run(self):
        self.assertGreater(len(approval_funnel(self.conn).table), 0)
        drift = applicant_drift(self.conn)
        dti = drift.table[drift.table.变量 == "DTI(%)"].iloc[0]
        self.assertGreater(float(dti["PSI(本期申请vs基期申请)"]), 0.10)  # built-in DTI drift


class LendingClubLoaderTests(unittest.TestCase):
    def test_load_small_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            acc, rej = tmp / "accepted.csv.gz", tmp / "rejected.csv"
            with gzip.open(acc, "wt", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id"] + lendingclub.ACCEPTED_COLS)
                w.writerow(["1", "Dec-2015", "10000", "690", "694", "18.5", "CA", "10+ years", "Fully Paid"])
                w.writerow(["2", "Jan-2016", "5000", "660", "664", "30.1", "TX", "< 1 year", "Charged Off"])
                w.writerow(["3", "Jan-2016", "8000", "700", "704", "", "NY", "n/a", "Current"])
                w.writerow(["Total amount funded in policy code 1: 23000"])
            with open(rej, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Amount Requested", "Application Date", "Loan Title", "Risk_Score",
                            "Debt-To-Income Ratio", "State", "Employment Length"])
                w.writerow(["3000", "2016-01-05", "x", "610", "22.58%", "CA", "3 years"])
                w.writerow(["1000", "2016-01-07", "y", "", "-1%", "TX", "< 1 year"])
            db = tmp / "lc.db"
            counts = lendingclub.load(acc, rej, db, log=lambda *_: None)
            self.assertEqual(counts, {"accepted": 3, "rejected": 2})
            df = pd.read_sql_query("SELECT * FROM applications", sqlite3.connect(db))
            self.assertEqual(df.app_month.tolist(), ["2015-12", "2016-01", "2016-01", "2016-01", "2016-01"])
            self.assertEqual(df.score.tolist()[:4], [692, 662, 702, 610])
            self.assertTrue(np.isnan(df.dti.iloc[4]))  # -1% means not provided
            self.assertEqual(df.emp_years.tolist()[:2], [10, 0])
            self.assertEqual(df.bad.tolist()[:2], [0, 1])
            self.assertTrue(np.isnan(df.bad.iloc[2]))


if __name__ == "__main__":
    unittest.main()
