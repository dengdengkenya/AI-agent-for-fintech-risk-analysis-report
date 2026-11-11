"""Deterministic analysis calculations and the facts the model may cite."""

import json
import unittest

from loan_analyst import calculations, report
from loan_analyst.tools import ToolContext, ToolUnavailable
from tests.fixtures import cube_rows, make_snapshot, scenario


class CutoffTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = make_snapshot()

    def compare(self, **changes):
        return calculations.compare_cutoffs(self.snapshot, scenario(**changes))

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
        self.assertIn("N/A", report.cutoff_table(sparse, scenario(min_outcomes=5)))
        self.assertIn("No tested cutoff has enough", report.cutoff_glance(sparse, scenario(min_outcomes=5)))

    def test_candidate_requires_ceiling_and_complete_financial_coverage(self):
        self.assertIsNone(self.compare()["candidate_cutoff"])
        self.assertEqual(self.compare(max_loss_rate=.1)["candidate_cutoff"], 700)
        self.assertEqual(self.compare(max_loss_rate=.25)["candidate_cutoff"], 600)
        rows = cube_rows()
        rows["accepted"][1]["terminal_rows"] = 3
        self.snapshot = make_snapshot(rows)
        self.assertIsNone(self.compare(max_loss_rate=.25)["candidate_cutoff"])

    def test_sweep_moves_the_candidate_with_the_ceiling(self):
        result = calculations.sweep_loss_ceilings(self.snapshot, scenario(max_loss_rate=.01), [.3, .1, .1])
        self.assertEqual([(row["ceiling"], row["candidate_cutoff"]) for row in result["ceilings"]],
                         [(.1, 700), (.3, 600)])
        facts = calculations.sweep_facts(result, scenario())
        self.assertIn("candidate cutoff 600 retains 5 accepted loans", facts["ceiling_2"])
        rows = cube_rows()
        rows["accepted"][1]["terminal_rows"] = 3
        none = calculations.sweep_loss_ceilings(make_snapshot(rows), scenario(), [.5])
        self.assertIn("no tested cutoff qualifies", calculations.sweep_facts(none, scenario())["ceiling_1"])

    def test_facts_keep_unavailable_values_and_only_explicit_fields(self):
        result = self.compare()
        result["password"] = result["scenarios"][0]["borrower"] = "private-marker"
        result["scenarios"][0]["loss_rate"] = None
        facts = calculations.cutoff_facts(result, scenario())
        self.assertIn("principal-loss proxy N/A", facts["accepted_600"])
        self.assertIn("no cutoff is selected", facts["selection"])
        self.assertNotIn("private-marker", json.dumps(facts))
        self.assertNotIn("SELECT", json.dumps(facts))

    def test_comparison_facts_separate_loss_from_financial_completeness(self):
        result = self.compare(baseline=600)
        result["scenarios"][-1]["loss_data_coverage"] = .8
        facts = calculations.cutoff_facts(result, scenario(baseline=600))
        self.assertIn("fewer accepted loans", facts["comparison_high"])
        self.assertIn("loss proxy is lower", facts["comparison_high"])
        self.assertIn("coverage at the highest cutoff is lower", facts["comparison_coverage"])
        self.assertIn("Financial coverage is completeness", facts["comparison_coverage"])

    def test_near_equal_loss_and_empty_cohort_are_not_meaningful_changes(self):
        result = self.compare()
        result["scenarios"][0]["loss_rate_change_pp"] = .0001
        self.assertIn("unchanged at the displayed precision",
                      calculations.cutoff_facts(result, scenario())["comparison_low"])
        empty_scenario = scenario(start_month="2030-01")
        facts = calculations.cutoff_facts(calculations.compare_cutoffs(self.snapshot, empty_scenario),
                                          empty_scenario)
        self.assertIn("unavailable", facts["comparison_high"])
        self.assertIn("unavailable", facts["comparison_coverage"])

    def test_hypothetical_inputs_are_labelled_as_assumptions(self):
        assumed = scenario(assume_comparable_scores=True, rejected_loss_rate=.15, take_up_rate=.25)
        result = calculations.compare_cutoffs(self.snapshot, assumed)
        self.assertIn("not measured outcomes", calculations.cutoff_facts(result, assumed)["hypothetical_inputs"])
        self.assertIn("hypothetical_inputs", calculations.cutoff_highlights(result, assumed))
        self.assertIn("Explicit hypothetical expansion", report.rejected_screen(result, assumed))


class OverviewTests(unittest.TestCase):
    def test_overview_describes_sources_and_cohort_exclusions(self):
        result = calculations.analyze_overview(make_snapshot(), scenario())
        self.assertEqual(result["sources"]["accepted"]["total_rows"], 34)
        self.assertEqual((result["sources"]["accepted"]["first_month"], result["sources"]["accepted"]["last_month"]),
                         ("2012-06", "2016-01"))
        facts = calculations.overview_facts(result)
        self.assertIn("optional segments: grade, term", facts["source_accepted"])
        self.assertIn("invalid scores", facts["quality_accepted"])
        self.assertIn("invalid-amount exclusions are 20.0000%", facts["quality_comparison"])
        self.assertIn("do not distinguish these reasons", facts["quality_comparison"])
        self.assertIn("not the repayment observation date", facts["snapshot"])
        empty = calculations.overview_facts(
            calculations.analyze_overview(make_snapshot(), scenario(start_month="2030-01")))
        self.assertIn("unavailable", empty["quality_comparison"])


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = make_snapshot()

    def segments(self, dimension, **options):
        return calculations.analyze_segments(self.snapshot, scenario(), dimension, **options)

    def test_score_bands_show_the_loans_each_cutoff_step_removes(self):
        result = self.segments("score_band")
        self.assertEqual([(item["segment"], item["accepted"]["rows"]) for item in result["segments"]],
                         [("below 600", 5), ("600-699", 3), ("700+", 2), ("invalid score", 11)])
        band = result["segments"][1]
        self.assertAlmostEqual(band["loss_rate"], .4)
        self.assertAlmostEqual(sum(item["loan_share"] for item in result["segments"]), 1)
        facts = calculations.segment_facts(result)
        self.assertIn("highest principal-loss proxy is 600-699 at 40.00%", facts["loss_range"])
        self.assertEqual(set(calculations.segment_highlights(result, facts)), set(facts))

    def test_issue_years_report_seasoning(self):
        result = self.segments("issue_year")
        self.assertEqual([item["segment"] for item in result["segments"]], ["2014", "2015", "2016"])
        facts = calculations.segment_facts(result)
        self.assertIn("ranges from 0.00% (issue year 2014) to 80.00% (issue year 2015)", facts["seasoning"])
        self.assertEqual(calculations.segment_highlights(result, facts), ("scope", "seasoning"))

    def test_grade_and_term_segments_and_score_filter(self):
        grades = self.segments("grade")
        self.assertEqual([item["segment"] for item in grades["segments"]], ["A", "C", "E", "invalid/missing"])
        terms = self.segments("term", min_score=600)
        self.assertEqual([(item["segment"], item["accepted"]["rows"]) for item in terms["segments"]],
                         [("36 months", 5)])
        self.assertIn("score at or above 600", calculations.segment_facts(terms)["scope"])

    def test_missing_optional_columns_are_reported_as_unavailable(self):
        snapshot = make_snapshot(dimensions=("term",))
        with self.assertRaises(ToolUnavailable):
            calculations.analyze_segments(snapshot, scenario(), "grade")
        self.assertTrue(calculations.analyze_segments(snapshot, scenario(), "term")["segments"])


class PortfolioRiskTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = make_snapshot()

    def test_vintages_compare_only_seasoned_years(self):
        result = calculations.analyze_vintages(self.snapshot, scenario())
        self.assertEqual([(item["vintage"], item["seasoned"]) for item in result["vintages"]],
                         [("2014", False), ("2015", True), ("2016", False)])
        self.assertIsNone(result["seasoned_change"])  # one seasoned vintage is not a comparison
        facts = calculations.vintage_facts(result)
        self.assertIn("Fewer than two vintages are seasoned at the 80.00% threshold", facts["seasoned_range"])
        self.assertNotIn("seasoned_change", facts)
        self.assertIn("2 of 3 vintages are below the 80.00% seasoning threshold: 2014, 2016.", facts["unseasoned"])
        self.assertIn("principal-loss proxy N/A; not seasoned", facts["vintage_2014"])  # open loans are not 0%
        self.assertEqual(calculations.vintage_highlights(facts), ("scope", "seasoned_range", "unseasoned"))

    def test_quarterly_vintages_report_the_change_in_percentage_points(self):
        result = calculations.analyze_vintages(self.snapshot, scenario(), "quarter", min_terminal_share=.5)
        self.assertEqual(result["comparable"], ["2015-Q1", "2015-Q2"])
        self.assertAlmostEqual(result["seasoned_change"]["change_pp"], -40)
        facts = calculations.vintage_facts(result)
        self.assertIn("highest principal-loss proxy is 2015-Q1 at 40.00% and the lowest is 2015-Q2 at 0.00%",
                      facts["seasoned_range"])
        self.assertIn("is lower by 40.00 percentage points (40.00% versus 0.00%)", facts["seasoned_change"])
        self.assertIn("Issue quarter 2015-Q1: 3 loans; terminal share 66.67%", facts["vintage_2015_q1"])
        table = report.vintage_table(result)
        self.assertIn("| 2014-Q1 | 5 | 0.00% | no | N/A | N/A |", table)
        with self.assertRaises(ValueError):
            calculations.analyze_vintages(self.snapshot, scenario(), "decade")

    def test_concentration_compares_loss_share_with_principal_share(self):
        result = calculations.analyze_concentration(self.snapshot, scenario(), "score_band")
        self.assertEqual((result["complete_loans"], result["principal"], result["loss_amount"]), (4, 400, 80))
        segments = {item["segment"]: item for item in result["segments"]}
        self.assertEqual((segments["600-699"]["principal_share"], segments["600-699"]["loss_share"],
                          segments["600-699"]["loss_to_principal_ratio"]), (.5, 1, 2))
        self.assertEqual(segments["700+"]["loss_to_principal_ratio"], 0)
        self.assertIsNone(segments["below 600"]["loss_to_principal_ratio"])  # no complete loans: N/A, not zero
        self.assertEqual([item["segment"] for item in result["top"]], ["600-699"])
        facts = calculations.concentration_facts(result)
        self.assertIn("50.00% of funded principal and 100.00% of unrecovered principal", facts["score_band_600_699"])
        self.assertIn("its loss share is 2.00 times its principal share", facts["score_band_600_699"])
        self.assertIn("ratio of loss share to principal share is N/A", facts["score_band_below_600"])
        self.assertIn("largest contributor to unrecovered principal is score band 600-699 with 100.00%", facts["top"])
        self.assertIn("600-699 carries the most loss (2.00 times its principal share) and 700+ the least (0.00",
                      facts["overweight"])
        self.assertIn("| 600-699 | 2 | 50.00% | 100.00% | 2.00 |", report.concentration_table(result))

    def test_concentration_without_losses_or_columns_is_unavailable(self):
        no_loss = calculations.analyze_concentration(self.snapshot, scenario(), "grade", min_score=700)
        self.assertIsNone(no_loss["segments"][0]["loss_share"])
        facts = calculations.concentration_facts(no_loss)
        self.assertIn("loss shares are unavailable", facts["top"])
        self.assertNotIn("overweight", facts)
        snapshot = make_snapshot(dimensions=("term",))
        for analyze in (calculations.analyze_concentration, calculations.analyze_mix):
            with self.subTest(analyze=analyze), self.assertRaises(ToolUnavailable):
                analyze(snapshot, scenario(), "grade")

    def test_mix_follows_shares_and_average_score_across_issue_years(self):
        result = calculations.analyze_mix(self.snapshot, scenario(end_month="2015-12"), "score_band")
        self.assertEqual(result["segments"], ["below 600", "600-699", "700+"])
        self.assertEqual([(year["year"], year["loans"]) for year in result["years"]], [("2014", 5), ("2015", 5)])
        self.assertAlmostEqual(result["years"][1]["average_score"], 668.8)
        self.assertAlmostEqual(result["average_score_change"], 88.8)
        facts = calculations.mix_facts(result)
        self.assertIn("score band mix: below 600 0.00%, 600-699 60.00%, 700+ 40.00%.", facts["year_2015"])
        self.assertIn("600-699 was 0.00% of issued loans in 2014 and 60.00% in 2015, a change of +60.00 percentage",
                      facts["change_600_699"])
        self.assertIn("largest change in mix is score band below 600: 100.00% to 0.00%", facts["largest_shift"])
        self.assertIn("was 580.0 in 2014 and 668.8 in 2015, a change of +88.8 points", facts["average_score"])
        self.assertIn("| 2015 | 5 | 668.8 | 0.00% | 60.00% | 40.00% |", report.mix_table(result))

    def test_mix_without_scores_or_a_second_year_stays_unavailable(self):
        unscored = calculations.mix_facts(calculations.analyze_mix(self.snapshot, scenario(), "term"))
        self.assertNotIn("average_score", unscored)  # 2016 has no valid score: no change is invented
        self.assertIn("average score N/A", unscored["year_2016"])
        one_year = calculations.analyze_mix(self.snapshot, scenario(start_month="2015-01", end_month="2015-12"),
                                            "score_band")
        self.assertEqual(one_year["changes"], [])
        self.assertIn("fewer than two issue years", calculations.mix_facts(one_year)["largest_shift"])
        with self.assertRaises(ValueError):
            calculations.analyze_mix(self.snapshot, scenario(), "issue_year")

    def test_new_tools_run_through_the_registry_with_json_safe_results(self):
        registry = calculations.build_registry()
        context = ToolContext(self.snapshot, scenario())
        for name, arguments in (("vintage_trend", {"period": "quarter", "min_terminal_share": .5}),
                                ("loss_concentration", {"dimension": "term"}),
                                ("mix_shift", {"dimension": "grade"})):
            with self.subTest(tool=name):
                envelope = registry.call(name, arguments, context)
                self.assertTrue(envelope["ok"], envelope)
                output = envelope["output"]
                self.assertTrue(set(output.highlights) <= set(output.facts))
                json.dumps(output.result, allow_nan=False)
        self.assertEqual(registry.call("vintage_trend", {"min_terminal_share": 80}, context)["error"]["code"],
                         "invalid_arguments")
        self.assertEqual(registry.call("mix_shift", {"dimension": "issue_year"}, context)["error"]["code"],
                         "invalid_arguments")


class RejectedTests(unittest.TestCase):
    def test_score_eras_keep_fico_and_vantagescore_apart(self):
        result = calculations.analyze_rejected(make_snapshot(), scenario())
        groups = {group["group"]: group for group in result["groups"]}
        fico, vantage = groups["FICO era (before 2013-11)"], groups["VantageScore era (after 2013-11)"]
        self.assertEqual((fico["rows"], vantage["rows"]), (4, 1))
        at_700 = {group: next(screen for screen in groups[group]["screens"] if screen["cutoff"] == 700)
                  for group in groups}
        self.assertEqual(at_700["VantageScore era (after 2013-11)"]["amount_sum"], 400)
        self.assertEqual(at_700["FICO era (before 2013-11)"]["rows"], 0)
        facts = calculations.rejected_facts(result)
        self.assertIn("not comparable to accepted FICO", facts["scope"])
        years = calculations.analyze_rejected(make_snapshot(), scenario(), by="application_year")
        self.assertEqual([group["group"] for group in years["groups"]], ["2013", "2015"])
        self.assertEqual(calculations.era("2013-11"), "transition")


if __name__ == "__main__":
    unittest.main()
