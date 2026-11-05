"""Score cutoff comparison: retained historical loans, losses and coverage per cutoff."""

from ..cube import ACCEPTED, REJECTED
from ..formatting import number, pct, signed, table, usd
from ..tools import Tool, ToolOutput, object_schema
from .common import (COHORT_PARAMS, CUTOFF_PARAMS, LOSS_CEILING_PARAM, MIN_OUTCOMES_PARAM, performance,
                     quality, ratio, scenario_for, select_candidate, total)


def analyze(snapshot, scenario):
    bands = {kind: snapshot.cubes[kind].bands(scenario.cutoffs, scenario.start_month, scenario.end_month)
             for kind in (ACCEPTED, REJECTED)}
    return analyze_bands(bands, scenario)


def analyze_bands(aggregates, scenario):
    """Combine score bands, compare each cutoff to baseline, then select a candidate.

    A band holds a disjoint score range. A cutoff includes that band and every
    higher band, so its counts overlap with those of other cutoffs.
    """
    scored_accepted = sum(row["rows"] for row in aggregates[ACCEPTED]
                          if row["scope"] == "in_scope" and row["bucket"] >= 0)
    scenarios = []
    for cutoff in scenario.cutoffs:
        selected = _totals_at_cutoff(aggregates, cutoff)
        accepted = selected[ACCEPTED]
        rejected = selected[REJECTED]
        scenarios.append({
            "cutoff": cutoff, "accepted": accepted, "rejected": rejected,
            "accepted_retention_rate": ratio(accepted["rows"], scored_accepted),
            **performance(accepted, scenario.min_outcomes),
            **_expansion_assumptions(rejected, scenario),
        })

    # Settings always include the baseline among the tested cutoffs.
    baseline = next(row for row in scenarios if row["cutoff"] == scenario.baseline)
    for row in scenarios:
        _add_baseline_comparison(row, baseline, scenario.assume_comparable_scores)

    candidate = select_candidate(scenarios, scenario.max_loss_rate)
    return {"quality": {kind: quality(rows) for kind, rows in aggregates.items()}, "scenarios": scenarios,
            "baseline": scenario.baseline, "candidate_cutoff": candidate["cutoff"] if candidate else None}


def _totals_at_cutoff(aggregates, cutoff):
    """Count only in-cohort bands that meet the cutoff; invalid scores are excluded."""
    selected = {}
    for kind, bands in aggregates.items():
        retained_bands = [
            band for band in bands
            if band["scope"] == "in_scope" and band["bucket"] >= cutoff
        ]
        selected[kind] = total(retained_bands)
    return selected


def _expansion_assumptions(rejected, scenario):
    """Optional user assumptions; rejected applications have no observed repayments."""
    if not scenario.assume_comparable_scores:
        return {"assumed_new_loans": None, "assumed_new_principal": None, "assumed_new_loss": None}

    # Only applications with valid requested amounts enter this hypothetical lending.
    principal = rejected["amount_sum"] * scenario.take_up_rate
    return {
        "assumed_new_loans": rejected["amount_rows"] * scenario.take_up_rate,
        "assumed_new_principal": principal,
        "assumed_new_loss": principal * scenario.rejected_loss_rate,
    }


def _add_baseline_comparison(row, baseline, include_expansion):
    """Add differences to one cutoff row, using the accepted-only baseline."""
    accepted = row["accepted"]
    baseline_accepted = baseline["accepted"]
    row["accepted_count_change"] = accepted["rows"] - baseline_accepted["rows"]
    row["accepted_volume_change"] = accepted["amount_sum"] - baseline_accepted["amount_sum"]
    row["accepted_growth_rate"] = ratio(row["accepted_count_change"], baseline_accepted["rows"])

    row["loss_rate_change_pp"] = None
    if row["loss_rate"] is not None and baseline["loss_rate"] is not None:
        # Rates are fractions; multiplying their difference by 100 gives percentage points.
        row["loss_rate_change_pp"] = 100 * (row["loss_rate"] - baseline["loss_rate"])

    if include_expansion:
        row["hypothetical_total_loans"] = accepted["rows"] + row["assumed_new_loans"]
        row["hypothetical_count_change"] = row["hypothetical_total_loans"] - baseline_accepted["rows"]


def _baseline(result):
    return next(row for row in result["scenarios"] if row["cutoff"] == result["baseline"])


def facts(result, scenario):
    """Deterministic sentences; every number the model may quote is written here."""
    rows, baseline = result["scenarios"], _baseline(result)
    candidate = result["candidate_cutoff"]
    facts = {
        "cohort": (f"Cohort: {scenario.cohort}. Comparison baseline: {scenario.baseline}; not a verified lending "
                   f"policy. Minimum relevant outcomes for a displayed rate: {scenario.min_outcomes}."),
        "selection": (
            "No loss ceiling was supplied; no cutoff is selected." if scenario.max_loss_rate is None else
            f"Historical loss ceiling for this comparison: {pct(scenario.max_loss_rate)}. Candidate cutoff: "
            f"{candidate if candidate is not None else 'none'}. Selection maximizes retained accepted count "
            "subject to the ceiling, enough outcomes and complete terminal financial coverage. "
            "It is not an approval recommendation."
        ),
    }
    for label, row in (("low", rows[0]), ("high", rows[-1])):
        direction = ("more" if row["accepted_count_change"] > 0 else
                     "fewer" if row["accepted_count_change"] < 0 else "the same number of")
        change = row["loss_rate_change_pp"]
        if change is None:
            loss = "The loss comparison is unavailable."
        elif abs(change) < .005:
            loss = "The loss proxy is unchanged at the displayed precision; do not claim a meaningful loss change."
        else:
            loss = f"The loss proxy is {'higher' if change > 0 else 'lower'} by {abs(change):.2f} percentage points."
        facts[f"comparison_{label}"] = (
            f"The {'lowest' if label == 'low' else 'highest'} tested cutoff {row['cutoff']} retains {direction} "
            f"accepted loans than baseline {scenario.baseline}: change {row['accepted_count_change']:+,} "
            f"({pct(row['accepted_growth_rate'])}). {loss} This compares cumulative historical groups; it does "
            "not prove a causal policy effect or a monotonic relationship across every tested cutoff."
        )
    base_coverage, high_coverage = baseline["loss_data_coverage"], rows[-1]["loss_data_coverage"]
    if base_coverage is None or high_coverage is None:
        coverage = "The baseline-to-highest terminal financial coverage comparison is unavailable."
    else:
        direction = ("lower" if high_coverage < base_coverage else
                     "higher" if high_coverage > base_coverage else "unchanged")
        coverage = (f"Terminal financial coverage at the highest cutoff is {direction} than/relative to baseline "
                    f"({high_coverage:.4%} versus {base_coverage:.4%}).")
    facts["comparison_coverage"] = coverage + (
        " Financial coverage is completeness, not the loss rate. Falling loss rates do not imply improving coverage.")
    for row in rows:
        accepted, rejected = row["accepted"], row["rejected"]
        change = "N/A" if row["loss_rate_change_pp"] is None else f"{row['loss_rate_change_pp']:+.2f} pp"
        facts[f"accepted_{row['cutoff']}"] = (
            f"Accepted cutoff {row['cutoff']}: {accepted['rows']:,} retained loans; "
            f"{usd(accepted['amount_sum'])} funded principal; {row['accepted_count_change']:+,} loans vs baseline; "
            f"terminal share {pct(row['terminal_share'])}; terminal charge-off rate {pct(row['chargeoff_rate'])}; "
            f"principal-loss proxy {pct(row['loss_rate'])}; loss change {change} vs baseline; "
            f"{accepted['performance_rows']:,} complete terminal outcomes; "
            f"terminal financial coverage {pct(row['loss_data_coverage'])}."
        )
        facts[f"rejected_{row['cutoff']}"] = (
            f"Rejected numeric threshold {row['cutoff']}: {rejected['rows']:,} screened applications; "
            f"{usd(rejected['amount_sum'])} valid requested amount across {rejected['amount_rows']:,} applications."
        )
    if scenario.assume_comparable_scores:
        facts["hypothetical_inputs"] = (
            f"Explicit user assumptions, not measured outcomes: equal score scales, {pct(scenario.take_up_rate)} "
            f"take-up, {pct(scenario.rejected_loss_rate)} lifetime rejected loss. "
            "These do not establish future performance."
        )
    return facts


def highlights(result, scenario):
    keys = ["cohort", "selection", "comparison_low", "comparison_high", "comparison_coverage",
            f"accepted_{scenario.baseline}"]
    if result["candidate_cutoff"] is not None:
        keys.append(f"accepted_{result['candidate_cutoff']}")
    return tuple(dict.fromkeys(keys + (["hypothetical_inputs"] if scenario.assume_comparable_scores else [])))


def glance(result, scenario):
    """Calculated summary bullets; no model text."""
    rows, baseline = result["scenarios"], _baseline(result)
    lines = [f"- The comparison baseline is **{scenario.baseline}** (a scenario assumption, not a verified current "
             f"policy). It retains **{baseline['accepted']['rows']:,} accepted loans** and "
             f"**{usd(baseline['accepted']['amount_sum'])}** of valid funded principal. Its historical "
             f"principal-loss proxy is **{pct(baseline['loss_rate'])}** on complete terminal outcomes."]
    for label, row in (("Lowest tested cutoff", rows[0]), ("Highest tested cutoff", rows[-1])):
        if row["cutoff"] != scenario.baseline:
            lines.append(f"- {label} **{row['cutoff']}** retains **{row['accepted']['rows']:,} loans**, a change of "
                         f"**{row['accepted_count_change']:+,}** ({pct(row['accepted_growth_rate'])}) versus "
                         f"baseline, with a historical loss proxy of **{pct(row['loss_rate'])}**.")
    if scenario.max_loss_rate is None:
        lines.append("- No risk ceiling was supplied, so no cutoff is selected. "
                     "Use the tables to agree a volume/loss tradeoff.")
    elif result["candidate_cutoff"] is None:
        lines.append(f"- No tested cutoff clears the supplied **{pct(scenario.max_loss_rate)}** historical loss "
                     "ceiling with enough outcomes and complete terminal financial data.")
    else:
        lines.append(f"- **{result['candidate_cutoff']}** retains the most historical accepted loans among tested "
                     f"cutoffs with a loss proxy at or below **{pct(scenario.max_loss_rate)}**, enough outcomes, "
                     "and complete terminal financial data. This is a historical screening candidate, not a "
                     "lending-policy recommendation; hypothetical expansion losses are not tested against it.")
    if not any(row["accepted"]["rows"] for row in rows):
        lines.append("- **No accepted loans with usable scores meet the tested cutoffs in this cohort.** "
                     "There is no supported accepted growth/loss comparison.")
    elif not any(row["loss_rate"] is not None for row in rows):
        lines.append("- **No tested cutoff has enough complete terminal outcomes for a loss rate.** "
                     "Volume alone cannot establish the risk tradeoff.")
    return "\n".join(lines)


def cutoff_table(result, scenario):
    rows = []
    for row in result["scenarios"]:
        delta = "N/A" if row["loss_rate_change_pp"] is None else f"{row['loss_rate_change_pp']:+.2f}"
        marker = " (baseline)" if row["cutoff"] == scenario.baseline else ""
        rows.append([f"{row['cutoff']}{marker}", f"{row['accepted']['rows']:,}", f"{row['accepted_count_change']:+,}",
                     usd(row["accepted"]["amount_sum"]), pct(row["terminal_share"]), pct(row["chargeoff_rate"]),
                     pct(row["loss_rate"]), delta])
    return ("Each row retains loans whose original FICO midpoint is at or above the cutoff. Counts include open "
            "loans; losses use only Fully Paid and Charged Off loans. These populations differ, so the table is a "
            "tradeoff screen, not a forecast.\n\n" + table(
                ["Cutoff", "Retained loans", "Change vs baseline", "Funded principal", "Terminal share",
                 "Charge-off rate", "Loss proxy", "Loss change (pp)"], rows))


def rejected_screen(result, scenario):
    lines = [table(["Numeric threshold", "Screened applications", "Valid requested amount",
                    "Applications with valid amount"],
                   [[row["cutoff"], f"{row['rejected']['rows']:,}", usd(row["rejected"]["amount_sum"]),
                     f"{row['rejected']['amount_rows']:,}"] for row in result["scenarios"]])]
    if scenario.assume_comparable_scores:
        lines += ["", "#### Explicit hypothetical expansion", "",
                  f"Assumes comparable scores, **{pct(scenario.take_up_rate)}** take-up of valid requested amounts, "
                  f"full requested funding, and **{pct(scenario.rejected_loss_rate)}** lifetime principal loss on "
                  "that new lending. Neither take-up nor losses are estimated from rejected outcomes.", "",
                  table(["Cutoff", "Assumed new loans", "Assumed new principal", "Assumed new losses",
                         "Total loan change vs accepted-only baseline"],
                        [[row["cutoff"], number(row["assumed_new_loans"]), usd(row["assumed_new_principal"]),
                          usd(row["assumed_new_loss"]), signed(row["hypothetical_count_change"])]
                         for row in result["scenarios"]]), "",
                  "New-loss assumptions and historical accepted losses have different observation windows; they are "
                  "not added into a portfolio loss forecast. Expansion includes all screened rejects at each cutoff, "
                  "including the baseline; it is not solely the effect of lowering a threshold."]
    else:
        lines += ["", "No rejected loan growth or loss forecast is made because rejected applications have no "
                      "observed repayment outcomes."]
    return "\n".join(lines)


def coverage_by_cutoff(result, scenario):
    return table(["Cutoff", "Excluded nonterminal/other outcomes", "Complete terminal outcomes",
                  "Terminal financial coverage", "Loss denominator", "Observed loss proxy dollars"],
                 [[row["cutoff"], f"{row['accepted']['rows'] - row['accepted']['terminal_rows']:,}",
                   f"{row['accepted']['performance_rows']:,}", pct(row["loss_data_coverage"]),
                   usd(row["accepted"]["performance_principal"]), usd(row["accepted"]["loss_amount"])]
                  for row in result["scenarios"]])


def sweep(snapshot, scenario, ceilings):
    """Screening candidate for each loss ceiling, from one cutoff comparison."""
    base = analyze(snapshot, scenario.with_overrides(max_loss_rate=None))
    baseline = _baseline(base)
    rows = []
    for ceiling in sorted(set(ceilings)):
        candidate = select_candidate(base["scenarios"], ceiling)
        rows.append({"ceiling": ceiling, "candidate_cutoff": candidate["cutoff"] if candidate else None,
                     "retained_loans": candidate["accepted"]["rows"] if candidate else None,
                     "accepted_growth_rate": candidate["accepted_growth_rate"] if candidate else None,
                     "loss_rate": candidate["loss_rate"] if candidate else None})
    return {"baseline": scenario.baseline, "baseline_loss_rate": baseline["loss_rate"],
            "cutoffs": list(scenario.cutoffs), "ceilings": rows}


def sweep_facts(result, scenario):
    facts = {"rule": (f"Ceiling sweep over cutoffs {', '.join(map(str, result['cutoffs']))} for cohort "
                      f"{scenario.cohort}; baseline {result['baseline']} has a principal-loss proxy of "
                      f"{pct(result['baseline_loss_rate'])}. Each ceiling is an analysis input, not a policy.")}
    for index, row in enumerate(result["ceilings"], start=1):
        if row["candidate_cutoff"] is None:
            facts[f"ceiling_{index}"] = (
                f"With a {pct(row['ceiling'])} historical loss ceiling, no tested cutoff qualifies: none has a loss "
                f"proxy at or below it with at least {scenario.min_outcomes} complete outcomes and 100% terminal "
                "financial coverage.")
        else:
            facts[f"ceiling_{index}"] = (
                f"With a {pct(row['ceiling'])} historical loss ceiling, candidate cutoff {row['candidate_cutoff']} "
                f"retains {row['retained_loans']:,} accepted loans ({pct(row['accepted_growth_rate'])} vs baseline "
                f"{result['baseline']}) with a principal-loss proxy of {pct(row['loss_rate'])}.")
    return facts


def _compare(context, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze(context.snapshot, scenario)
    title = f"Cutoffs {', '.join(map(str, scenario.cutoffs))} vs baseline {scenario.baseline}, cohort {scenario.cohort}"
    if scenario.max_loss_rate is not None:
        title += f", ceiling {pct(scenario.max_loss_rate)}: candidate {result['candidate_cutoff'] or 'none'}"
    return ToolOutput(title, result, facts(result, scenario), highlights(result, scenario))


def _sweep(context, ceilings, **arguments):
    scenario = scenario_for(context, arguments)
    result = sweep(context.snapshot, scenario, ceilings)
    found = sweep_facts(result, scenario)
    return ToolOutput(f"Loss ceiling sweep {', '.join(pct(c) for c in sorted(set(ceilings)))}, cohort "
                      f"{scenario.cohort}", result, found, tuple(found))


TOOLS = (
    Tool("compare_score_cutoffs",
         "Compare cumulative score cutoffs on historical accepted loans: retained loans, funded principal, "
         "terminal share, charge-off rate and principal-loss proxy versus the baseline, plus the rejected numeric "
         "screen. Give max_loss_rate to find a historical screening candidate. Omitted arguments use the session "
         "settings.",
         object_schema({**CUTOFF_PARAMS, **COHORT_PARAMS, **LOSS_CEILING_PARAM, **MIN_OUTCOMES_PARAM}), _compare),
    Tool("sweep_loss_ceilings",
         "For each historical loss ceiling, find the tested cutoff that retains the most accepted loans while "
         "meeting the ceiling with enough outcomes and complete terminal financial data. Shows how the screening "
         "candidate moves as the ceiling changes; not a policy recommendation.",
         object_schema({
             "ceilings": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                          "minItems": 1, "maxItems": 10,
                          "description": "Loss ceilings as fractions, for example [0.06, 0.08, 0.1]."},
             **CUTOFF_PARAMS, **COHORT_PARAMS, **MIN_OUTCOMES_PARAM}, required=["ceilings"]), _sweep),
)
