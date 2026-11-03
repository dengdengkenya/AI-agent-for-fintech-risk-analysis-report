"""Deterministic analysis calculations and the facts the model may cite."""

import json
import unittest

from loan_analyst.analyses import cutoffs, overview, rejected, segments
from loan_analyst.tools import ToolUnavailable
from tests.fixtures import cube_rows, make_snapshot, scenario


class CutoffTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = make_snapshot()

    def compare(self, **changes):
        return cutoffs.analyze(self.snapshot, scenario(**changes))

    def test_cumulative_cutoffs_and_weighted_loss_denominator(self):
        lower, baseline = self.compare()["scenarios"]
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
        quality = self.compare()["quality"]["accepted"]
        self.assertEqual((quality["total_rows"], quality["in_scope_rows"], quality["invalid_date_rows"],
                          quality["outside_period_rows"], quality["invalid_score_rows"],
                          quality["invalid_amount_rows"]), (34, 21, 7, 6, 11, 1))
        self.assertEqual((quality["first_month"], quality["last_month"]), ("2014-02", "2016-01"))

    def test_rejects_do_not_silently_become_approvals_or_losses(self):
        row = self.compare()["scenarios"][0]
        self.assertEqual((row["rejected"]["rows"], row["rejected"]["amount_rows"]), (5, 4))
        for field in ("assumed_new_loans", "assumed_new_principal", "assumed_new_loss"):
            self.assertIsNone(row[field])
        self.assertNotIn("hypothetical_total_loans", row)

    def test_expansion_uses_only_valid_amounts_and_explicit_assumptions(self):
        lower, baseline = self.compare(assume_comparable_scores=True, rejected_loss_rate=.2,
                                       take_up_rate=.25)["scenarios"]
        self.assertEqual((lower["assumed_new_loans"], lower["assumed_new_principal"], lower["assumed_new_loss"]),
                         (1, 250, 50))
        self.assertEqual((lower["hypothetical_total_loans"], lower["hypothetical_count_change"]), (6, 4))
        # Expansion is measured against the accepted-only baseline, even at the baseline cutoff.
        self.assertEqual(baseline["hypothetical_count_change"], .25)
        self.assertEqual(lower["loss_rate"], .2)

    def test_empty_and_sparse_outcomes_are_unavailable_not_zero(self):
        empty = self.compare(start_month="2030-01")
        for row in empty["scenarios"]:
            for field in ("loss_rate", "chargeoff_rate", "accepted_growth_rate", "terminal_share",
                          "loss_rate_change_pp", "loss_data_coverage"):
                self.assertIsNone(row[field], field)
        sparse = self.compare(min_outcomes=5)
        self.assertTrue(all(row["loss_rate"] is None and row["chargeoff_rate"] is None
                            for row in sparse["scenarios"]))
        self.assertIn("N/A", cutoffs.cutoff_table(sparse, scenario(min_outcomes=5)))
        self.assertIn("No tested cutoff has enough", cutoffs.glance(sparse, scenario(min_outcomes=5)))

    def test_candidate_requires_ceiling_and_complete_financial_coverage(self):
        self.assertIsNone(self.compare()["candidate_cutoff"])
        self.assertEqual(self.compare(max_loss_rate=.1)["candidate_cutoff"], 700)
        self.assertEqual(self.compare(max_loss_rate=.25)["candidate_cutoff"], 600)
        rows = cube_rows()
        rows["accepted"][1]["terminal_rows"] = 3
        self.snapshot = make_snapshot(rows)
        self.assertIsNone(self.compare(max_loss_rate=.25)["candidate_cutoff"])

    def test_sweep_moves_the_candidate_with_the_ceiling(self):
        result = cutoffs.sweep(self.snapshot, scenario(max_loss_rate=.01), [.3, .1, .1])
        self.assertEqual([(row["ceiling"], row["candidate_cutoff"]) for row in result["ceilings"]],
                         [(.1, 700), (.3, 600)])
        facts = cutoffs.sweep_facts(result, scenario())
        self.assertIn("candidate cutoff 600 retains 5 accepted loans", facts["ceiling_2"])
        rows = cube_rows()
        rows["accepted"][1]["terminal_rows"] = 3
        none = cutoffs.sweep(make_snapshot(rows), scenario(), [.5])
        self.assertIn("no tested cutoff qualifies", cutoffs.sweep_facts(none, scenario())["ceiling_1"])

    def test_facts_keep_unavailable_values_and_only_explicit_fields(self):
        result = self.compare()
        result["password"] = result["scenarios"][0]["borrower"] = "private-marker"
        result["scenarios"][0]["loss_rate"] = None
        facts = cutoffs.facts(result, scenario())
        self.assertIn("principal-loss proxy N/A", facts["accepted_600"])
        self.assertIn("no cutoff is selected", facts["selection"])
        self.assertNotIn("private-marker", json.dumps(facts))
        self.assertNotIn("SELECT", json.dumps(facts))

    def test_comparison_facts_separate_loss_from_financial_completeness(self):
        result = self.compare(baseline=600)
        result["scenarios"][-1]["loss_data_coverage"] = .8
        facts = cutoffs.facts(result, scenario(baseline=600))
        self.assertIn("fewer accepted loans", facts["comparison_high"])
        self.assertIn("loss proxy is lower", facts["comparison_high"])
        self.assertIn("coverage at the highest cutoff is lower", facts["comparison_coverage"])
        self.assertIn("Financial coverage is completeness", facts["comparison_coverage"])

    def test_near_equal_loss_and_empty_cohort_are_not_meaningful_changes(self):
        result = self.compare()
        result["scenarios"][0]["loss_rate_change_pp"] = .0001
        self.assertIn("unchanged at the displayed precision", cutoffs.facts(result, scenario())["comparison_low"])
        empty_scenario = scenario(start_month="2030-01")
        facts = cutoffs.facts(cutoffs.analyze(self.snapshot, empty_scenario), empty_scenario)
        self.assertIn("unavailable", facts["comparison_high"])
        self.assertIn("unavailable", facts["comparison_coverage"])

    def test_hypothetical_inputs_are_labelled_as_assumptions(self):
        assumed = scenario(assume_comparable_scores=True, rejected_loss_rate=.15, take_up_rate=.25)
        result = cutoffs.analyze(self.snapshot, assumed)
        self.assertIn("not measured outcomes", cutoffs.facts(result, assumed)["hypothetical_inputs"])
        self.assertIn("hypothetical_inputs", cutoffs.highlights(result, assumed))
        self.assertIn("Explicit hypothetical expansion", cutoffs.rejected_screen(result, assumed))


class OverviewTests(unittest.TestCase):
    def test_overview_describes_sources_and_cohort_exclusions(self):
        result = overview.analyze(make_snapshot(), scenario())
        self.assertEqual(result["sources"]["accepted"]["total_rows"], 34)
        self.assertEqual((result["sources"]["accepted"]["first_month"], result["sources"]["accepted"]["last_month"]),
                         ("2012-06", "2016-01"))
        facts = overview.facts(result, scenario())
        self.assertIn("optional segments: grade, term", facts["source_accepted"])
        self.assertIn("invalid scores", facts["quality_accepted"])
        self.assertIn("invalid-amount exclusions are 20.0000%", facts["quality_comparison"])
        self.assertIn("do not distinguish these reasons", facts["quality_comparison"])
        self.assertIn("not the repayment observation date", facts["snapshot"])
        empty = overview.facts(overview.analyze(make_snapshot(), scenario(start_month="2030-01")), scenario())
        self.assertIn("unavailable", empty["quality_comparison"])


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = make_snapshot()

    def segments(self, dimension, **options):
        return segments.analyze(self.snapshot, scenario(), dimension, **options)

    def test_score_bands_show_the_loans_each_cutoff_step_removes(self):
        result = self.segments("score_band")
        self.assertEqual([(item["segment"], item["accepted"]["rows"]) for item in result["segments"]],
                         [("below 600", 5), ("600-699", 3), ("700+", 2), ("invalid score", 11)])
        band = result["segments"][1]
        self.assertAlmostEqual(band["loss_rate"], .4)
        self.assertAlmostEqual(sum(item["loan_share"] for item in result["segments"]), 1)
        facts = segments.facts(result)
        self.assertIn("highest principal-loss proxy is 600-699 at 40.00%", facts["loss_range"])
        self.assertEqual(set(segments.highlights(result, facts)), set(facts))

    def test_issue_years_report_seasoning(self):
        result = self.segments("issue_year")
        self.assertEqual([item["segment"] for item in result["segments"]], ["2014", "2015", "2016"])
        facts = segments.facts(result)
        self.assertIn("ranges from 0.00% (issue year 2014) to 80.00% (issue year 2015)", facts["seasoning"])
        self.assertEqual(segments.highlights(result, facts), ("scope", "seasoning"))

    def test_grade_and_term_segments_and_score_filter(self):
        grades = self.segments("grade")
        self.assertEqual([item["segment"] for item in grades["segments"]], ["A", "C", "E", "invalid/missing"])
        terms = self.segments("term", min_score=600)
        self.assertEqual([(item["segment"], item["accepted"]["rows"]) for item in terms["segments"]],
                         [("36 months", 5)])
        self.assertIn("score at or above 600", segments.facts(terms)["scope"])

    def test_missing_optional_columns_are_reported_as_unavailable(self):
        snapshot = make_snapshot(dimensions=("term",))
        with self.assertRaises(ToolUnavailable):
            segments.analyze(snapshot, scenario(), "grade")
        self.assertTrue(segments.analyze(snapshot, scenario(), "term")["segments"])


class RejectedTests(unittest.TestCase):
    def test_score_eras_keep_fico_and_vantagescore_apart(self):
        result = rejected.analyze(make_snapshot(), scenario())
        groups = {group["group"]: group for group in result["groups"]}
        fico, vantage = groups["FICO era (before 2013-11)"], groups["VantageScore era (after 2013-11)"]
        self.assertEqual((fico["rows"], vantage["rows"]), (4, 1))
        at_700 = {group: next(screen for screen in groups[group]["screens"] if screen["cutoff"] == 700)
                  for group in groups}
        self.assertEqual(at_700["VantageScore era (after 2013-11)"]["amount_sum"], 400)
        self.assertEqual(at_700["FICO era (before 2013-11)"]["rows"], 0)
        facts = rejected.facts(result)
        self.assertIn("not comparable to accepted FICO", facts["scope"])
        years = rejected.analyze(make_snapshot(), scenario(), by="application_year")
        self.assertEqual([group["group"] for group in years["groups"]], ["2013", "2015"])
        self.assertEqual(rejected.era("2013-11"), "transition")


if __name__ == "__main__":
    unittest.main()
