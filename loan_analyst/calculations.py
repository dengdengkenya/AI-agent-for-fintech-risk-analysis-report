"""All calculations behind the report, and the tools the model may call.

Read one section at a time:

  1. Shared rate rules          total(), performance(), quality(), candidate
  2. Data overview              analyze_overview(), overview_facts()
  3. Score cutoffs             compare_cutoffs(), cutoff_facts(), sweep
  4. Segment breakdowns        analyze_segments(), segment_facts()
  5. Rejected applications     analyze_rejected(), rejected_facts()
  6. Portfolio risk views      analyze_vintages(), analyze_concentration(), analyze_mix()
  7. Citable definitions       DEFINITIONS
  8. Tool catalog              schemas, handlers, build_registry()

Each analysis follows the same shape:

    analyze_xxx(snapshot, scenario) -> a plain dict of results
    xxx_facts(result, ...)          -> short sentences the model can cite
    xxx_tool(context, ...)          -> wraps both in a ToolOutput

The rule running through every calculation: a missing value is ``None`` and is
shown as N/A. It is never silently turned into zero.
"""

import math
import re

from .settings import MONTH_PATTERN
from .snapshot import ACCEPTED, REJECTED, SCORE_SYSTEM_CHANGE, SOURCES, bucket
from .support import pct, usd
from .tools import Tool, ToolOutput, ToolRegistry, ToolUnavailable, object_schema

SUM_FIELDS = ("rows", "amount_rows", "amount_sum", "terminal_rows", "charged_off_rows",
              "performance_rows", "performance_principal", "loss_amount")
SCENARIO_KEYS = ("cutoffs", "baseline", "start_month", "end_month", "max_loss_rate", "min_outcomes")


# ===========================================================================
# Shared rate rules
# ===========================================================================

def ratio(numerator, denominator):
    """Return None (unavailable) when there is no denominator."""
    return numerator / denominator if denominator else None


def total(rows):
    """Add counts exactly and money with correctly rounded sums.

    Rejected applications have no terminal or loss fields. Those absent fields
    are structural zeros, not measured loan outcomes.
    """
    totals = {}
    for field in SUM_FIELDS:
        values = (row.get(field, 0) for row in rows)
        if field.endswith("rows"):
            totals[field] = sum(values)
        else:
            totals[field] = math.fsum(values)
    return totals


def performance(totals, min_outcomes):
    """Calculate the three rates and the coverage measure for one group of loans.

    Terminal loans are exactly Fully Paid or Charged Off. The loss proxy uses
    only terminal loans with complete financial data; its denominator is funded
    principal, not a loan count. Too few outcomes makes a rate unavailable.
    """
    terminal_count = totals["terminal_rows"]
    complete_count = totals["performance_rows"]

    chargeoff_rate = None
    if terminal_count >= min_outcomes:
        chargeoff_rate = ratio(totals["charged_off_rows"], terminal_count)

    loss_rate = None
    if complete_count >= min_outcomes:
        loss_rate = ratio(totals["loss_amount"], totals["performance_principal"])

    return {
        "terminal_share": ratio(terminal_count, totals["rows"]),
        "chargeoff_rate": chargeoff_rate,
        "loss_rate": loss_rate,
        "loss_data_coverage": ratio(complete_count, terminal_count),
    }


def quality(bands):
    """Coverage and exclusions for one source, from its disjoint (scope, bucket) bands."""
    scoped = [row for row in bands if row["scope"] == "in_scope"]

    dates = []
    for row in scoped:
        for key in ("first_month", "last_month"):
            if row.get(key):
                dates.append(row[key])

    invalid_score_rows = 0
    invalid_amount_rows = 0
    for row in scoped:
        if row["bucket"] == -1:
            invalid_score_rows += row["rows"]
        invalid_amount_rows += row["rows"] - row["amount_rows"]

    return {
        "total_rows": sum(row["rows"] for row in bands),
        "in_scope_rows": sum(row["rows"] for row in scoped),
        "invalid_date_rows": sum(row["rows"] for row in bands if row["scope"] == "invalid_date"),
        "outside_period_rows": sum(row["rows"] for row in bands if row["scope"] == "outside_period"),
        "invalid_score_rows": invalid_score_rows,
        "invalid_amount_rows": invalid_amount_rows,
        "first_month": min(dates) if dates else None,
        "last_month": max(dates) if dates else None,
    }


def select_candidate(scenarios, ceiling):
    """Most retained accepted loans among cutoffs that meet the loss ceiling.

    A candidate must also have at least the minimum number of complete outcomes
    and 100% terminal financial coverage. Without a ceiling, there is no
    candidate at all.
    """
    if ceiling is None:
        return None

    eligible = []
    for row in scenarios:
        # performance() already withholds loss rates with too few outcomes.
        if row["loss_rate"] is None:
            continue
        if row["loss_rate"] <= ceiling and row["loss_data_coverage"] == 1:
            eligible.append(row)

    # When retained counts tie, keep the higher cutoff.
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["accepted"]["rows"], row["cutoff"]))


def baseline_row(result):
    """Find the scenario row for the configured baseline cutoff."""
    for row in result["scenarios"]:
        if row["cutoff"] == result["baseline"]:
            return row
    raise LookupError("baseline cutoff is not among the tested cutoffs")


def slug(text):
    """Make a safe fact key from a label such as 'VantageScore era'."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "value"


def scenario_for(context, arguments):
    """Apply validated tool arguments to the session scenario; cross-field rules rerun."""
    changes = {}
    for key in SCENARIO_KEYS:
        if key in arguments:
            changes[key] = arguments[key]
    return context.scenario.with_overrides(**changes)


# ===========================================================================
# 1. Data overview
# ===========================================================================

def analyze_overview(snapshot, scenario):
    """Describe the snapshot: tables, row counts, month ranges and exclusions."""
    sources = {}
    for kind in SOURCES:
        cube = snapshot.cubes[kind]
        first, last = cube.month_bounds()
        sources[kind] = {
            "table": snapshot.sources[kind]["table"],
            "total_rows": cube.total_rows(),
            "first_month": first,
            "last_month": last,
            "dimensions": list(snapshot.sources[kind]["dimensions"]),
        }

    quality_by_source = {}
    for kind in SOURCES:
        bands = snapshot.cubes[kind].bands(scenario.cutoffs, scenario.start_month,
                                           scenario.end_month)
        quality_by_source[kind] = quality(bands)

    return {
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "database": snapshot.database,
            "read_started_at_utc": snapshot.read_started_at_utc,
            "read_completed_at_utc": snapshot.read_completed_at_utc,
        },
        "cohort": {"start_month": scenario.start_month, "end_month": scenario.end_month},
        "sources": sources,
        "quality": quality_by_source,
    }


def overview_facts(result):
    """Citable facts about the snapshot and its cohort exclusions."""
    snapshot = result["snapshot"]
    facts = {"snapshot": (
        f"Snapshot {snapshot['snapshot_id']} was read from MySQL schema {snapshot['database']} between "
        f"{snapshot['read_started_at_utc']} and {snapshot['read_completed_at_utc']} in one read-only "
        "transaction. Read time is not the repayment observation date."
    )}

    for kind, months in ((ACCEPTED, "issue"), (REJECTED, "application")):
        source = result["sources"][kind]
        segments = (f"; optional segments: {', '.join(source['dimensions']) or 'none'}"
                    if kind == ACCEPTED else "")
        facts[f"source_{kind}"] = (
            f"{kind.capitalize()} source {snapshot['database']}.{source['table']}: {source['total_rows']:,} rows; "
            f"parseable {months} months {source['first_month'] or 'unavailable'} to "
            f"{source['last_month'] or 'unavailable'}{segments}."
        )

    for kind, source_quality in result["quality"].items():
        facts[f"quality_{kind}"] = (
            f"{kind}: {source_quality['total_rows']:,} table rows; "
            f"{source_quality['in_scope_rows']:,} in cohort; "
            f"{source_quality['invalid_date_rows']:,} invalid dates; "
            f"{source_quality['outside_period_rows']:,} outside cohort; "
            f"{source_quality['invalid_score_rows']:,} invalid scores and "
            f"{source_quality['invalid_amount_rows']:,} invalid amounts within cohort. "
            f"Observed bounds: {source_quality['first_month']} to {source_quality['last_month']}."
        )

    rejected = result["quality"][REJECTED]
    if rejected["in_scope_rows"]:
        facts["quality_comparison"] = (
            f"Rejected invalid-score exclusions are "
            f"{rejected['invalid_score_rows'] / rejected['in_scope_rows']:.4%} "
            f"of cohort rows; invalid-amount exclusions are "
            f"{rejected['invalid_amount_rows'] / rejected['in_scope_rows']:.4%}. "
            "Judge each issue by its own fraction; they may overlap and must not be added. "
            "Invalid scores include missing, malformed, noninteger and out-of-range values; "
            "the aggregates do not distinguish these reasons or prove the source scores are erroneous."
        )
    else:
        facts["quality_comparison"] = "No rejected rows are in scope; exclusion shares are unavailable."
    return facts


# ===========================================================================
# 2. Score cutoffs
# ===========================================================================

def compare_cutoffs(snapshot, scenario):
    """Replay every cutoff on historical accepted loans and compare it with the baseline.

    A score band holds a disjoint range of scores. A cutoff retains that band
    and every higher band, so cutoff totals overlap each other.
    """
    # Step 1: one grouped summary per source, split into (cohort status, score band).
    aggregates = {}
    for kind in SOURCES:
        aggregates[kind] = snapshot.cubes[kind].bands(
            scenario.cutoffs, scenario.start_month, scenario.end_month)

    # Step 2: denominator for "what share of scored accepted loans does this cut off keep?"
    scored_accepted = 0
    for band in aggregates[ACCEPTED]:
        if band["scope"] == "in_scope" and band["bucket"] >= 0:
            scored_accepted += band["rows"]

    # Step 3: add up the retained bands for each cutoff and calculate its rates.
    scenarios = []
    for cutoff in scenario.cutoffs:
        selected = {}
        for kind in SOURCES:
            retained_bands = []
            for band in aggregates[kind]:
                if band["scope"] == "in_scope" and band["bucket"] >= cutoff:
                    retained_bands.append(band)
            selected[kind] = total(retained_bands)

        accepted = selected[ACCEPTED]
        rejected = selected[REJECTED]

        # Optional user assumption: treat rejected scores as comparable and
        # estimate new lending. These are not measured outcomes.
        if scenario.assume_comparable_scores:
            assumed_new_principal = rejected["amount_sum"] * scenario.take_up_rate
            assumed_new_loans = rejected["amount_rows"] * scenario.take_up_rate
            assumed_new_loss = assumed_new_principal * scenario.rejected_loss_rate
        else:
            assumed_new_loans = None
            assumed_new_principal = None
            assumed_new_loss = None

        row = {
            "cutoff": cutoff,
            "accepted": accepted,
            "rejected": rejected,
            "accepted_retention_rate": ratio(accepted["rows"], scored_accepted),
            **performance(accepted, scenario.min_outcomes),
            "assumed_new_loans": assumed_new_loans,
            "assumed_new_principal": assumed_new_principal,
            "assumed_new_loss": assumed_new_loss,
        }
        scenarios.append(row)

    # Step 4: describe each cutoff relative to the baseline, then pick a candidate.
    baseline = baseline_row({"scenarios": scenarios, "baseline": scenario.baseline})
    for row in scenarios:
        add_baseline_comparison(row, baseline, scenario.assume_comparable_scores)

    candidate = select_candidate(scenarios, scenario.max_loss_rate)
    quality_by_source = {}
    for kind in SOURCES:
        quality_by_source[kind] = quality(aggregates[kind])

    return {
        "quality": quality_by_source,
        "scenarios": scenarios,
        "baseline": scenario.baseline,
        "candidate_cutoff": candidate["cutoff"] if candidate else None,
    }


def add_baseline_comparison(row, baseline, include_expansion):
    """Add differences versus the accepted-only baseline to one cutoff row."""
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


def cutoff_facts(result, scenario):
    """Deterministic sentences; every number the model may quote is written here."""
    rows = result["scenarios"]
    baseline = baseline_row(result)
    candidate = result["candidate_cutoff"]

    if scenario.max_loss_rate is None:
        selection = "No loss ceiling was supplied; no cutoff is selected."
    else:
        selection = (
            f"Historical loss ceiling for this comparison: {pct(scenario.max_loss_rate)}. "
            f"Candidate cutoff: {candidate if candidate is not None else 'none'}. "
            "Selection maximizes retained accepted count subject to the ceiling, enough "
            "outcomes and complete terminal financial coverage. It is not an approval "
            "recommendation."
        )

    facts = {
        "cohort": (
            f"Cohort: {scenario.cohort}. Comparison baseline: {scenario.baseline}; not a verified lending "
            f"policy. Minimum relevant outcomes for a displayed rate: {scenario.min_outcomes}."
        ),
        "selection": selection,
    }

    for label, row in (("low", rows[0]), ("high", rows[-1])):
        if row["accepted_count_change"] > 0:
            direction = "more"
        elif row["accepted_count_change"] < 0:
            direction = "fewer"
        else:
            direction = "the same number of"

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

    base_coverage = baseline["loss_data_coverage"]
    high_coverage = rows[-1]["loss_data_coverage"]
    if base_coverage is None or high_coverage is None:
        coverage = "The baseline-to-highest terminal financial coverage comparison is unavailable."
    else:
        if high_coverage < base_coverage:
            direction = "lower"
        elif high_coverage > base_coverage:
            direction = "higher"
        else:
            direction = "unchanged"
        coverage = (f"Terminal financial coverage at the highest cutoff is {direction} than/relative to baseline "
                    f"({high_coverage:.4%} versus {base_coverage:.4%}).")
    facts["comparison_coverage"] = coverage + (
        " Financial coverage is completeness, not the loss rate. Falling loss rates do not imply improving coverage.")

    for row in rows:
        accepted = row["accepted"]
        rejected = row["rejected"]
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


def cutoff_highlights(result, scenario):
    keys = ["cohort", "selection", "comparison_low", "comparison_high", "comparison_coverage",
            f"accepted_{scenario.baseline}"]
    if result["candidate_cutoff"] is not None:
        keys.append(f"accepted_{result['candidate_cutoff']}")
    if scenario.assume_comparable_scores:
        keys.append("hypothetical_inputs")
    return tuple(dict.fromkeys(keys))


def sweep_loss_ceilings(snapshot, scenario, ceilings):
    """Screening candidate for each loss ceiling, from one cutoff comparison."""
    base = compare_cutoffs(snapshot, scenario.with_overrides(max_loss_rate=None))
    baseline = baseline_row(base)

    rows = []
    for ceiling in sorted(set(ceilings)):
        candidate = select_candidate(base["scenarios"], ceiling)
        rows.append({
            "ceiling": ceiling,
            "candidate_cutoff": candidate["cutoff"] if candidate else None,
            "retained_loans": candidate["accepted"]["rows"] if candidate else None,
            "accepted_growth_rate": candidate["accepted_growth_rate"] if candidate else None,
            "loss_rate": candidate["loss_rate"] if candidate else None,
        })

    return {
        "baseline": scenario.baseline,
        "baseline_loss_rate": baseline["loss_rate"],
        "cutoffs": list(scenario.cutoffs),
        "ceilings": rows,
    }


def sweep_facts(result, scenario):
    facts = {"rule": (
        f"Ceiling sweep over cutoffs {', '.join(map(str, result['cutoffs']))} for cohort "
        f"{scenario.cohort}; baseline {result['baseline']} has a principal-loss proxy of "
        f"{pct(result['baseline_loss_rate'])}. Each ceiling is an analysis input, not a policy."
    )}

    for index, row in enumerate(result["ceilings"], start=1):
        if row["candidate_cutoff"] is None:
            facts[f"ceiling_{index}"] = (
                f"With a {pct(row['ceiling'])} historical loss ceiling, no tested cutoff qualifies: none has a loss "
                f"proxy at or below it with at least {scenario.min_outcomes} complete outcomes and 100% terminal "
                "financial coverage."
            )
        else:
            facts[f"ceiling_{index}"] = (
                f"With a {pct(row['ceiling'])} historical loss ceiling, candidate cutoff {row['candidate_cutoff']} "
                f"retains {row['retained_loans']:,} accepted loans ({pct(row['accepted_growth_rate'])} vs baseline "
                f"{result['baseline']}) with a principal-loss proxy of {pct(row['loss_rate'])}."
            )
    return facts


# ===========================================================================
# 3. Segment breakdowns
# ===========================================================================

TITLES = {"score_band": "Score band", "issue_year": "Issue year", "grade": "Grade", "term": "Term"}
MISSING = "invalid/missing"


def band_label(band, cutoffs):
    """Human label for a score bucket: below 600, 600-699, 700+, or invalid."""
    if band == -1:
        return "invalid score"
    if band == 0:
        return f"below {cutoffs[0]}"
    index = cutoffs.index(band)
    if index == len(cutoffs) - 1:
        return f"{band}+"
    return f"{band}-{cutoffs[index + 1] - 1}"


def require_dimension(cube, dimension):
    """Refuse an unknown dimension, or one whose optional column this snapshot lacks."""
    if dimension not in TITLES:
        raise ValueError(f"Unknown dimension: {dimension}")
    if dimension in ("grade", "term") and dimension not in cube.dimensions:
        raise ToolUnavailable(f"{dimension} is unavailable: the accepted table has no {dimension} column")


def group_by_dimension(rows, dimension, cutoffs):
    """Split in-cohort rows into disjoint segments; return [(label, rows)] in display order."""
    groups = {}
    for row in rows:
        if dimension == "score_band":
            key = bucket(row["score"], cutoffs)
        elif dimension == "issue_year":
            key = row["period"][:4]
        else:
            key = row[dimension] or MISSING
        groups.setdefault(key, []).append(row)

    if dimension == "score_band":
        ordered_keys = sorted(groups, key=lambda key: (key == -1, key))
    elif dimension == "issue_year":
        ordered_keys = sorted(groups)
    else:
        ordered_keys = sorted(groups, key=lambda key: (key == MISSING, key))

    labelled = []
    for key in ordered_keys:
        if dimension == "score_band":
            label = band_label(key, cutoffs)
        elif dimension == "term" and key != MISSING:
            label = f"{key} months"
        else:
            label = str(key)
        labelled.append((label, groups[key]))
    return labelled


def analyze_segments(snapshot, scenario, dimension, min_score=None):
    """Filter the cohort, group its rows, then calculate each group's share and rates."""
    cube = snapshot.cubes[ACCEPTED]
    require_dimension(cube, dimension)

    rows = cube.select(scenario.start_month, scenario.end_month, min_score=min_score)

    loans = sum(row["rows"] for row in rows)
    segments = []
    for label, group_rows in group_by_dimension(rows, dimension, scenario.cutoffs):
        group_totals = total(group_rows)
        segments.append({
            "segment": label,
            "accepted": group_totals,
            "loan_share": ratio(group_totals["rows"], loans),
            **performance(group_totals, scenario.min_outcomes),
        })

    return {
        "dimension": dimension,
        "min_score": min_score,
        "cohort": scenario.cohort,
        "min_outcomes": scenario.min_outcomes,
        "loans": loans,
        "segments": segments,
    }


def segment_facts(result):
    name = TITLES[result["dimension"]]
    if result["min_score"] is None:
        scope = "all loans in the cohort"
    else:
        scope = f"loans with a valid score at or above {result['min_score']}"

    scope_text = (
        f"{name} breakdown of historical accepted loans: cohort {result['cohort']}; {scope}; {result['loans']:,} "
        f"loans. Rates need at least {result['min_outcomes']} relevant outcomes. Segments are descriptive "
        "historical groups, not causal effects."
    )
    if result["dimension"] == "score_band":
        scope_text += (" Each score band holds only loans between adjacent cutoffs, so it shows the loans each "
                       "cutoff step removes.")
    facts = {"scope": scope_text}

    for segment in result["segments"]:
        accepted = segment["accepted"]
        facts[f"{slug(name)}_{slug(segment['segment'])}"] = (
            f"{name} {segment['segment']}: {accepted['rows']:,} loans ({pct(segment['loan_share'])} of this view); "
            f"{usd(accepted['amount_sum'])} funded principal; terminal share {pct(segment['terminal_share'])}; "
            f"charge-off rate {pct(segment['chargeoff_rate'])}; principal-loss proxy {pct(segment['loss_rate'])}; "
            f"terminal financial coverage {pct(segment['loss_data_coverage'])}."
        )

    rated = [segment for segment in result["segments"] if segment["loss_rate"] is not None]
    if len(rated) >= 2:
        high = max(rated, key=lambda segment: segment["loss_rate"])
        low = min(rated, key=lambda segment: segment["loss_rate"])
        facts["loss_range"] = (
            f"Among {name.lower()} segments with enough outcomes, the highest principal-loss proxy is "
            f"{high['segment']} at {pct(high['loss_rate'])} and the lowest is {low['segment']} at "
            f"{pct(low['loss_rate'])}. This describes historical groups, not causes."
        )

    shares = [segment for segment in result["segments"] if segment["terminal_share"] is not None]
    if result["dimension"] == "issue_year" and len(shares) >= 2:
        low = min(shares, key=lambda segment: segment["terminal_share"])
        high = max(shares, key=lambda segment: segment["terminal_share"])
        facts["seasoning"] = (
            f"Terminal share ranges from {pct(low['terminal_share'])} (issue year {low['segment']}) to "
            f"{pct(high['terminal_share'])} (issue year {high['segment']}). Issue years with a low terminal share "
            "were mostly still open in the source data, so their loss proxies cover only resolved loans."
        )
    return facts


def segment_highlights(result, facts):
    if result["dimension"] == "score_band":
        return tuple(facts)  # bands are few and drive the marginal cutoff tradeoff
    return tuple(key for key in ("scope", "loss_range", "seasoning") if key in facts)


# ===========================================================================
# 4. Rejected applications
# ===========================================================================

ERAS = {
    "fico": f"FICO era (before {SCORE_SYSTEM_CHANGE})",
    "transition": f"{SCORE_SYSTEM_CHANGE} transition month (mixed score systems)",
    "vantage": f"VantageScore era (after {SCORE_SYSTEM_CHANGE})",
}
GROUPINGS = {"score_era": "score era", "application_year": "application year"}


def era(period):
    """Keep the changeover month separate because a monthly aggregate mixes systems."""
    if period < SCORE_SYSTEM_CHANGE:
        return "fico"
    if period == SCORE_SYSTEM_CHANGE:
        return "transition"
    return "vantage"


def screen_totals(rows):
    """The three totals a rejected-application numeric screen needs."""
    values = total(rows)
    return {key: values[key] for key in ("rows", "amount_rows", "amount_sum")}


def analyze_rejected(snapshot, scenario, by="score_era"):
    """Count numeric screens within each era or year, without inferring loan outcomes."""
    rows = snapshot.cubes[REJECTED].select(scenario.start_month, scenario.end_month)

    groups = {}
    for row in rows:
        if by == "score_era":
            key = era(row["period"])
        else:
            key = row["period"][:4]
        groups.setdefault(key, []).append(row)

    if by == "score_era":
        ordered_keys = list(ERAS)
    else:
        ordered_keys = sorted(groups)

    result = []
    for key in ordered_keys:
        if key not in groups:
            continue
        members = groups[key]
        usable = [row for row in members if row["score"] is not None]

        screens = []
        for cutoff in scenario.cutoffs:
            screened = [row for row in usable if row["score"] >= cutoff]
            screens.append({"cutoff": cutoff, **screen_totals(screened)})

        result.append({
            "group": ERAS[key] if by == "score_era" else key,
            **screen_totals(members),
            "usable_score_rows": sum(row["rows"] for row in usable),
            "screens": screens,
        })

    return {
        "by": by,
        "cohort": scenario.cohort,
        "baseline": scenario.baseline,
        "cutoffs": list(scenario.cutoffs),
        "groups": result,
    }


def screen_at_baseline(group, baseline):
    """The numeric screen for one rejected group at the baseline cutoff."""
    for screen in group["screens"]:
        if screen["cutoff"] == baseline:
            return screen
    raise LookupError("baseline cutoff is not among the tested cutoffs")


def rejected_facts(result):
    facts = {"scope": (
        f"Rejected applications grouped by {GROUPINGS[result['by']]}; cohort {result['cohort']}. A numeric screen "
        "counts applications whose Risk_Score is at or above a threshold. Risk_Score is FICO before "
        f"{SCORE_SYSTEM_CHANGE} and VantageScore after ({SCORE_SYSTEM_CHANGE} mixes both); it is not comparable to "
        "accepted FICO, and rejected applications have no repayment outcomes."
    )}

    for group in result["groups"]:
        screen = screen_at_baseline(group, result["baseline"])
        facts[slug(group["group"])] = (
            f"{group['group']}: {group['rows']:,} rejected applications in the cohort; "
            f"{group['usable_score_rows']:,} with a usable numeric score. At or above {result['baseline']}: "
            f"{screen['rows']:,} applications ({pct(ratio(screen['rows'], group['usable_score_rows']))} of usable) "
            f"with {usd(screen['amount_sum'])} valid requested amount."
        )
    return facts


# ===========================================================================
# 5. Portfolio risk views: vintage trend, loss concentration, origination mix
# ===========================================================================
#
# The model cannot do arithmetic it is allowed to quote, so every comparison a
# reader might want (a change in percentage points, a share, a ratio) is
# calculated here and written into a fact.

PERIODS = {"year": "issue year", "quarter": "issue quarter"}
DEFAULT_SEASONING = 0.8
MIX_DIMENSIONS = ("score_band", "grade", "term")
LISTED_VINTAGES = 12  # longest list of vintage names written into one fact


def vintage_key(month, period):
    """'2015-08' -> '2015' or '2015-Q3'."""
    if period == "year":
        return month[:4]
    return f"{month[:4]}-Q{(int(month[5:7]) - 1) // 3 + 1}"


def analyze_vintages(snapshot, scenario, period="year", min_terminal_share=DEFAULT_SEASONING, min_score=None):
    """Performance by issue vintage, separating seasoned vintages from mostly open ones.

    A vintage is "seasoned" when its terminal share reaches the threshold. Loss
    proxies of unseasoned vintages cover only the loans that resolved early, so
    only seasoned vintages are compared with each other.
    """
    if period not in PERIODS:
        raise ValueError(f"Unknown period: {period}")
    rows = snapshot.cubes[ACCEPTED].select(scenario.start_month, scenario.end_month, min_score=min_score)

    groups = {}
    for row in rows:
        groups.setdefault(vintage_key(row["period"], period), []).append(row)

    loans = sum(row["rows"] for row in rows)
    vintages = []
    for key in sorted(groups):
        group_totals = total(groups[key])
        rates = performance(group_totals, scenario.min_outcomes)
        terminal_share = rates["terminal_share"]
        vintages.append({
            "vintage": key,
            "accepted": group_totals,
            "loan_share": ratio(group_totals["rows"], loans),
            **rates,
            "seasoned": terminal_share is not None and terminal_share >= min_terminal_share,
        })

    # Only seasoned vintages with a displayable loss proxy can be compared.
    comparable = [vintage for vintage in vintages if vintage["seasoned"] and vintage["loss_rate"] is not None]
    change = None
    if len(comparable) >= 2:
        first, last = comparable[0], comparable[-1]
        change = {
            "first": first["vintage"], "first_loss_rate": first["loss_rate"],
            "last": last["vintage"], "last_loss_rate": last["loss_rate"],
            "change_pp": (last["loss_rate"] - first["loss_rate"]) * 100,
        }

    return {
        "period": period,
        "min_terminal_share": min_terminal_share,
        "min_score": min_score,
        "cohort": scenario.cohort,
        "min_outcomes": scenario.min_outcomes,
        "loans": loans,
        "vintages": vintages,
        "comparable": [vintage["vintage"] for vintage in comparable],
        "seasoned_change": change,
    }


def vintage_facts(result):
    name = PERIODS[result["period"]]
    threshold = pct(result["min_terminal_share"])
    scope = "all loans in the cohort" if result["min_score"] is None else (
        f"loans with a valid score at or above {result['min_score']}")
    facts = {"scope": (
        f"Vintage trend by {name}: cohort {result['cohort']}; {scope}; {result['loans']:,} loans. A vintage counts "
        f"as seasoned when at least {threshold} of its loans are resolved (Fully Paid or Charged Off). This "
        "threshold is a reporting choice, not a statistical test, and vintages are descriptive groups, not causes."
    )}

    vintages = result["vintages"]
    for vintage in vintages:
        status = "seasoned" if vintage["seasoned"] else "not seasoned, so its rates cover only resolved loans"
        facts[f"vintage_{slug(vintage['vintage'])}"] = (
            f"{name.capitalize()} {vintage['vintage']}: {vintage['accepted']['rows']:,} loans; terminal share "
            f"{pct(vintage['terminal_share'])}; charge-off rate {pct(vintage['chargeoff_rate'])}; principal-loss "
            f"proxy {pct(vintage['loss_rate'])}; {status}."
        )

    by_name = {vintage["vintage"]: vintage for vintage in vintages}
    comparable = [by_name[key] for key in result["comparable"]]
    if len(comparable) >= 2:
        high = max(comparable, key=lambda vintage: vintage["loss_rate"])
        low = min(comparable, key=lambda vintage: vintage["loss_rate"])
        facts["seasoned_range"] = (
            f"Among the {len(comparable)} seasoned vintages with enough outcomes, the highest principal-loss proxy "
            f"is {high['vintage']} at {pct(high['loss_rate'])} and the lowest is {low['vintage']} at "
            f"{pct(low['loss_rate'])}."
        )
        change = result["seasoned_change"]
        if abs(change["change_pp"]) < .005:
            movement = "is unchanged at the displayed precision"
        else:
            movement = (f"is {'higher' if change['change_pp'] > 0 else 'lower'} by "
                        f"{abs(change['change_pp']):.2f} percentage points")
        facts["seasoned_change"] = (
            f"From the earliest seasoned vintage {change['first']} to the latest seasoned vintage {change['last']}, "
            f"the principal-loss proxy {movement} ({pct(change['first_loss_rate'])} versus "
            f"{pct(change['last_loss_rate'])}). Differences in loan age and mix are not controlled."
        )
    else:
        facts["seasoned_range"] = (
            f"Fewer than two vintages are seasoned at the {threshold} threshold with enough outcomes, so no "
            "vintage-to-vintage loss comparison is made."
        )

    unseasoned = [vintage["vintage"] for vintage in vintages if not vintage["seasoned"]]
    if unseasoned:
        shown = ", ".join(unseasoned[:LISTED_VINTAGES])
        if len(unseasoned) > LISTED_VINTAGES:
            shown += " and later ones"
        facts["unseasoned"] = (
            f"{len(unseasoned)} of {len(vintages)} vintages are below the {threshold} seasoning threshold: {shown}. "
            "Their loss proxies cover only loans that resolved early and should not be compared with seasoned "
            "vintages."
        )
    else:
        facts["unseasoned"] = f"All {len(vintages)} vintages meet the {threshold} seasoning threshold."
    return facts


def vintage_highlights(facts):
    return tuple(key for key in ("scope", "seasoned_range", "seasoned_change", "unseasoned") if key in facts)


def analyze_concentration(snapshot, scenario, dimension, min_score=None):
    """Where historical unrecovered principal sits, compared with where principal was funded.

    Both shares use financially complete terminal loans only, the same population
    as the principal-loss proxy. A segment whose loss share exceeds its principal
    share carries more than a proportional part of the losses.
    """
    cube = snapshot.cubes[ACCEPTED]
    require_dimension(cube, dimension)
    rows = cube.select(scenario.start_month, scenario.end_month, min_score=min_score)

    overall = total(rows)
    segments = []
    for label, group_rows in group_by_dimension(rows, dimension, scenario.cutoffs):
        group_totals = total(group_rows)
        principal_share = ratio(group_totals["performance_principal"], overall["performance_principal"])
        loss_share = ratio(group_totals["loss_amount"], overall["loss_amount"])

        relative = None
        if principal_share and loss_share is not None and group_totals["performance_rows"] >= scenario.min_outcomes:
            relative = loss_share / principal_share

        segments.append({
            "segment": label,
            "complete_loans": group_totals["performance_rows"],
            "principal": group_totals["performance_principal"],
            "loss_amount": group_totals["loss_amount"],
            "principal_share": principal_share,
            "loss_share": loss_share,
            "loss_to_principal_ratio": relative,
        })

    # Largest contributors first; ties keep display order.
    ranked = sorted((segment for segment in segments if segment["loss_share"]),
                    key=lambda segment: -segment["loss_share"])
    top = []
    cumulative = 0.0
    for segment in ranked[:3]:
        cumulative += segment["loss_share"]
        top.append({"segment": segment["segment"], "loss_share": segment["loss_share"],
                    "cumulative_loss_share": cumulative})

    return {
        "dimension": dimension,
        "min_score": min_score,
        "cohort": scenario.cohort,
        "min_outcomes": scenario.min_outcomes,
        "complete_loans": overall["performance_rows"],
        "principal": overall["performance_principal"],
        "loss_amount": overall["loss_amount"],
        "segments": segments,
        "top": top,
    }


def concentration_facts(result):
    name = TITLES[result["dimension"]]
    scope = "all loans in the cohort" if result["min_score"] is None else (
        f"loans with a valid score at or above {result['min_score']}")
    facts = {"scope": (
        f"Loss concentration by {name.lower()}: cohort {result['cohort']}; {scope}. Shares use the "
        f"{result['complete_loans']:,} financially complete Fully Paid or Charged Off loans, with "
        f"{usd(result['principal'])} funded principal and {usd(result['loss_amount'])} unrecovered principal. "
        "Open loans are excluded. Shares show where historical losses sit; they are not limits, causes or forecasts."
    )}

    for segment in result["segments"]:
        relative = segment["loss_to_principal_ratio"]
        comparison = ("the ratio of loss share to principal share is N/A" if relative is None
                      else f"its loss share is {relative:.2f} times its principal share")
        facts[f"{slug(name)}_{slug(segment['segment'])}"] = (
            f"{name} {segment['segment']}: {pct(segment['principal_share'])} of funded principal and "
            f"{pct(segment['loss_share'])} of unrecovered principal among complete terminal loans; {comparison}."
        )

    top = result["top"]
    if top:
        text = (f"The largest contributor to unrecovered principal is {name.lower()} {top[0]['segment']} with "
                f"{pct(top[0]['loss_share'])}.")
        if len(top) > 1:
            names = ", ".join(item["segment"] for item in top)
            text += (f" The {len(top)} largest contributors ({names}) together hold "
                     f"{pct(top[-1]['cumulative_loss_share'])}.")
        facts["top"] = text
    else:
        facts["top"] = "No unrecovered principal is recorded for this view, so loss shares are unavailable."

    rated = [segment for segment in result["segments"] if segment["loss_to_principal_ratio"] is not None]
    if len(rated) >= 2:
        high = max(rated, key=lambda segment: segment["loss_to_principal_ratio"])
        low = min(rated, key=lambda segment: segment["loss_to_principal_ratio"])
        facts["overweight"] = (
            f"Relative to funded principal, {name.lower()} {high['segment']} carries the most loss "
            f"({high['loss_to_principal_ratio']:.2f} times its principal share) and {low['segment']} the least "
            f"({low['loss_to_principal_ratio']:.2f} times), among segments with at least "
            f"{result['min_outcomes']} complete outcomes."
        )
    return facts


def concentration_highlights(facts):
    return tuple(key for key in ("scope", "top", "overweight") if key in facts)


def average_score(rows):
    """Loan-weighted average of valid scores; None when no row has a valid score."""
    scored = [row for row in rows if row["score"] is not None]
    loans = sum(row["rows"] for row in scored)
    return ratio(math.fsum(row["score"] * row["rows"] for row in scored), loans)


def analyze_mix(snapshot, scenario, dimension):
    """How the make-up of issued loans changes from one issue year to the next.

    This counts every issued loan in the cohort, open or resolved, so it is not
    affected by seasoning. It describes composition only, not performance.
    """
    cube = snapshot.cubes[ACCEPTED]
    if dimension not in MIX_DIMENSIONS:
        raise ValueError(f"Unknown dimension: {dimension}")
    require_dimension(cube, dimension)
    rows = cube.select(scenario.start_month, scenario.end_month)

    # One fixed list of segments, so every year shows the same columns.
    labels = [label for label, _ in group_by_dimension(rows, dimension, scenario.cutoffs)]

    years = []
    for year, year_rows in group_by_dimension(rows, "issue_year", scenario.cutoffs):
        loans = sum(row["rows"] for row in year_rows)
        by_label = dict(group_by_dimension(year_rows, dimension, scenario.cutoffs))
        segments = []
        for label in labels:
            segment_loans = sum(row["rows"] for row in by_label.get(label, []))
            segments.append({"segment": label, "rows": segment_loans, "loan_share": ratio(segment_loans, loans)})
        years.append({"year": year, "loans": loans, "average_score": average_score(year_rows),
                      "segments": segments})

    changes = []
    score_change = None
    if len(years) >= 2:
        first, last = years[0], years[-1]
        for before, after in zip(first["segments"], last["segments"]):
            changes.append({
                "segment": before["segment"],
                "first_share": before["loan_share"],
                "last_share": after["loan_share"],
                "change_pp": (after["loan_share"] - before["loan_share"]) * 100,
            })
        if first["average_score"] is not None and last["average_score"] is not None:
            score_change = last["average_score"] - first["average_score"]

    return {
        "dimension": dimension,
        "cohort": scenario.cohort,
        "loans": sum(year["loans"] for year in years),
        "segments": labels,
        "years": years,
        "first_year": years[0]["year"] if years else None,
        "last_year": years[-1]["year"] if years else None,
        "changes": changes,
        "average_score_change": score_change,
    }


def _score(value):
    return "N/A" if value is None else f"{value:.1f}"


def mix_facts(result):
    name = TITLES[result["dimension"]]
    facts = {"scope": (
        f"Origination mix by {name.lower()} and issue year: cohort {result['cohort']}; {result['loans']:,} issued "
        "loans, open and resolved alike. Shares are of each year's issued loan count. A changing mix describes "
        "what was originated; it does not by itself explain or predict losses."
    )}

    for year in result["years"]:
        mix = ", ".join(f"{segment['segment']} {pct(segment['loan_share'])}" for segment in year["segments"])
        facts[f"year_{year['year']}"] = (
            f"Issue year {year['year']}: {year['loans']:,} issued loans; loan-weighted average score "
            f"{_score(year['average_score'])}; {name.lower()} mix: {mix}."
        )

    first, last = result["first_year"], result["last_year"]
    for change in result["changes"]:
        facts[f"change_{slug(change['segment'])}"] = (
            f"{name} {change['segment']} was {pct(change['first_share'])} of issued loans in {first} and "
            f"{pct(change['last_share'])} in {last}, a change of {change['change_pp']:+.2f} percentage points."
        )

    if result["changes"]:
        largest = max(result["changes"], key=lambda change: abs(change["change_pp"]))
        facts["largest_shift"] = (
            f"Between issue years {first} and {last}, the largest change in mix is {name.lower()} "
            f"{largest['segment']}: {pct(largest['first_share'])} to {pct(largest['last_share'])} of issued loans "
            f"({largest['change_pp']:+.2f} percentage points)."
        )
    else:
        facts["largest_shift"] = "The cohort holds fewer than two issue years, so no change in mix is calculated."

    if result["average_score_change"] is not None:
        years = result["years"]
        facts["average_score"] = (
            f"The loan-weighted average score of issued loans was {_score(years[0]['average_score'])} in {first} "
            f"and {_score(years[-1]['average_score'])} in {last}, a change of "
            f"{result['average_score_change']:+.1f} points. Loans without a valid score are excluded."
        )
    return facts


def mix_highlights(facts):
    return tuple(key for key in ("scope", "largest_shift", "average_score") if key in facts)


# ===========================================================================
# 6. Citable definitions
# ===========================================================================

DEFINITIONS = {
    "retained": (
        "A score cutoff comparison replays historical accepted loans: a cutoff retains the loans whose original "
        "FICO midpoint is at or above it. It does not measure approvals, rejections or a causal policy effect."
    ),
    "loss_proxy": (
        "Principal-loss proxy: unrecovered principal on financially complete Charged Off loans divided by funded "
        "principal of financially complete Fully Paid or Charged Off loans. Open and other statuses are excluded. "
        "This is historical, not a forecast, annualized default probability, profit or accounting net charge-off rate."
    ),
    "candidate_rule": (
        "A candidate cutoff needs an explicit loss ceiling, enough complete terminal outcomes and 100% terminal "
        "financial coverage; among qualifying tested cutoffs it retains the most historical accepted loans. "
        "It is a historical screening result, not an approval recommendation."
    ),
    "rejected_scores": (
        "Rejected Risk_Score mixes FICO and VantageScore, with unverified scale compatibility. Only numeric "
        "scores in 300-850 are screened. Rejected repayment outcomes are absent; screened counts cannot "
        "establish creditworthiness, approval rates or future losses."
    ),
    "source_limits": (
        "Month filters use issue/application cohorts, not repayment observation dates. Terminal-only selection "
        "and seasoning can bias losses. Missing data are excluded. These reads do not certify import "
        "completeness, deduplication or unique people."
    ),
    "next_checks": (
        "Useful next checks: agree a historical loss ceiling if one is absent; investigate why rejected scores "
        "fail the numeric screen and establish score-system comparability before interpreting demand; review "
        "excluded terminal financial records and loan-age differences before comparing performance."
    ),
    "seasoning": (
        "Seasoning: recent issue vintages have a low terminal share because most of their loans were still open "
        "in the source data. Their charge-off rates and loss proxies cover only loans that resolved early, so "
        "they are not comparable with older, mostly resolved vintages."
    ),
    "concentration": (
        "Loss concentration compares each segment's share of unrecovered principal with its share of funded "
        "principal, among financially complete terminal loans. A ratio above 1 means the segment carries more "
        "than a proportional share of historical losses. It is descriptive, not a limit breach or a forecast."
    ),
    "mix": (
        "Origination mix is the share of each issue year's issued loans in each segment. A shift in mix can "
        "change portfolio-level rates even when segment-level rates are stable; it does not by itself explain "
        "or predict losses."
    ),
}


# ===========================================================================
# 7. Tool parameters, handlers and the catalog
# ===========================================================================

# Omitted or null tool arguments use the session's settings.
_MONTH = {"type": "string", "pattern": f"^{MONTH_PATTERN}$", "maxLength": 7}
COHORT_PARAMS = {
    "start_month": {
        **_MONTH,
        "description": "Inclusive YYYY-MM cohort start (loan issue month, rejected "
                       "application month). Omit to keep the session cohort.",
    },
    "end_month": {
        **_MONTH,
        "description": "Inclusive YYYY-MM cohort end. Omit to keep the session cohort.",
    },
}
CUTOFF_PARAMS = {
    "cutoffs": {
        "type": "array",
        "items": {"type": "integer", "minimum": 300, "maximum": 850},
        "minItems": 1,
        "maxItems": 30,
        "description": "Inclusive minimum scores to compare; the baseline is added automatically.",
    },
    "baseline": {
        "type": "integer",
        "minimum": 300,
        "maximum": 850,
        "description": "Comparison cutoff; an illustrative reference, not a verified policy.",
    },
}
MIN_OUTCOMES_PARAM = {"min_outcomes": {
    "type": "integer", "minimum": 1,
    "description": "Minimum relevant terminal outcomes before a rate is shown; a reporting threshold."}}
LOSS_CEILING_PARAM = {"max_loss_rate": {
    "type": "number", "minimum": 0, "maximum": 1,
    "description": "Historical loss ceiling as a fraction (0.08 means 8%) used to pick a screening "
                   "candidate. Omit to keep the session ceiling, if any."}}


def describe_data_tool(context, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze_overview(context.snapshot, scenario)
    facts = overview_facts(result)
    return ToolOutput(f"Data overview, cohort {scenario.cohort}", result, facts, tuple(facts))


def compare_cutoffs_tool(context, **arguments):
    scenario = scenario_for(context, arguments)
    result = compare_cutoffs(context.snapshot, scenario)
    facts = cutoff_facts(result, scenario)

    title = (f"Cutoffs {', '.join(map(str, scenario.cutoffs))} vs baseline {scenario.baseline}, "
             f"cohort {scenario.cohort}")
    if scenario.max_loss_rate is not None:
        title += f", ceiling {pct(scenario.max_loss_rate)}: candidate {result['candidate_cutoff'] or 'none'}"
    return ToolOutput(title, result, facts, cutoff_highlights(result, scenario))


def sweep_loss_ceilings_tool(context, ceilings, **arguments):
    scenario = scenario_for(context, arguments)
    result = sweep_loss_ceilings(context.snapshot, scenario, ceilings)
    facts = sweep_facts(result, scenario)
    title = (f"Loss ceiling sweep {', '.join(pct(ceiling) for ceiling in sorted(set(ceilings)))}, "
             f"cohort {scenario.cohort}")
    return ToolOutput(title, result, facts, tuple(facts))


def segment_performance_tool(context, dimension, min_score=None, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze_segments(context.snapshot, scenario, dimension, min_score)
    facts = segment_facts(result)
    title = f"{TITLES[dimension]} breakdown, cohort {scenario.cohort}"
    if min_score is not None:
        title += f", score {min_score}+"
    return ToolOutput(title, result, facts, segment_highlights(result, facts))


def rejected_applications_tool(context, by="score_era", **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze_rejected(context.snapshot, scenario, by)
    facts = rejected_facts(result)
    return ToolOutput(f"Rejected applications by {GROUPINGS[by]}, cohort {scenario.cohort}",
                      result, facts, tuple(facts))


MIN_SCORE_PARAM = {"min_score": {
    "type": "integer", "minimum": 300, "maximum": 850,
    "description": "Only loans with a valid score at or above this cutoff. Omit for all loans in the cohort."}}


def vintage_trend_tool(context, period="year", min_terminal_share=DEFAULT_SEASONING, min_score=None,
                       **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze_vintages(context.snapshot, scenario, period, min_terminal_share, min_score)
    facts = vintage_facts(result)
    return ToolOutput(f"Vintage trend by {PERIODS[period]}, cohort {scenario.cohort}", result, facts,
                      vintage_highlights(facts))


def loss_concentration_tool(context, dimension, min_score=None, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze_concentration(context.snapshot, scenario, dimension, min_score)
    facts = concentration_facts(result)
    return ToolOutput(f"Loss concentration by {TITLES[dimension].lower()}, cohort {scenario.cohort}", result,
                      facts, concentration_highlights(facts))


def mix_shift_tool(context, dimension, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze_mix(context.snapshot, scenario, dimension)
    facts = mix_facts(result)
    return ToolOutput(f"Origination mix by {TITLES[dimension].lower()} and issue year, cohort {scenario.cohort}",
                      result, facts, mix_highlights(facts))


TOOLS = (
    Tool("describe_data",
         "Describe the loaded MySQL snapshot: source tables, row counts, parseable month ranges, optional "
         "segment columns, and exclusions (invalid dates, scores and amounts) for a cohort. Fast; use it "
         "for data-quality questions or to learn which months and segments exist.",
         object_schema(COHORT_PARAMS), describe_data_tool),

    Tool("compare_score_cutoffs",
         "Compare cumulative score cutoffs on historical accepted loans: retained loans, funded principal, "
         "terminal share, charge-off rate and principal-loss proxy versus the baseline, plus the rejected numeric "
         "screen. Give max_loss_rate to find a historical screening candidate. Omitted arguments use the session "
         "settings.",
         object_schema({**CUTOFF_PARAMS, **COHORT_PARAMS, **LOSS_CEILING_PARAM, **MIN_OUTCOMES_PARAM}),
         compare_cutoffs_tool),

    Tool("sweep_loss_ceilings",
         "For each historical loss ceiling, find the tested cutoff that retains the most accepted loans while "
         "meeting the ceiling with enough outcomes and complete terminal financial data. Shows how the screening "
         "candidate moves as the ceiling changes; not a policy recommendation.",
         object_schema({
             "ceilings": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                          "minItems": 1, "maxItems": 10,
                          "description": "Loss ceilings as fractions, for example [0.06, 0.08, 0.1]."},
             **CUTOFF_PARAMS, **COHORT_PARAMS, **MIN_OUTCOMES_PARAM,
         }, required=["ceilings"]),
         sweep_loss_ceilings_tool),

    Tool("segment_performance",
         "Break historical accepted-loan performance into disjoint segments: score_band (loans between adjacent "
         "cutoffs, showing what each cutoff step removes), issue_year (vintage and seasoning), grade, or term. "
         "Returns loans, share, funded principal, terminal share, charge-off rate, principal-loss proxy and "
         "financial coverage for each segment.",
         object_schema({
             "dimension": {"type": "string", "enum": list(TITLES), "description": "How to segment the loans."},
             "min_score": {"type": "integer", "minimum": 300, "maximum": 850,
                           "description": "Only loans with a valid score at or above this cutoff. Omit for all "
                                          "loans in the cohort."},
             "cutoffs": CUTOFF_PARAMS["cutoffs"],
             **COHORT_PARAMS,
             **MIN_OUTCOMES_PARAM,
         }, required=["dimension"]),
         segment_performance_tool),

    Tool("rejected_applications",
         "Profile rejected applications: counts and valid requested amounts at or above each numeric score "
         "threshold, grouped by score era (FICO before 2013-11, VantageScore after) or by application year. "
         "Rejected scores are not comparable to accepted FICO and have no repayment outcomes.",
         object_schema({"by": {"type": "string", "enum": list(GROUPINGS),
                               "description": "How to group applications."},
                        **CUTOFF_PARAMS, **COHORT_PARAMS}),
         rejected_applications_tool),

    Tool("vintage_trend",
         "Compare historical accepted-loan performance across issue vintages (year or quarter), marking which "
         "vintages are seasoned enough to compare. Returns each vintage's terminal share, charge-off rate and "
         "principal-loss proxy, the highest and lowest seasoned vintages, and the change between the earliest "
         "and latest seasoned vintage in percentage points.",
         object_schema({
             "period": {"type": "string", "enum": list(PERIODS), "description": "Vintage size. Default year."},
             "min_terminal_share": {"type": "number", "minimum": 0, "maximum": 1,
                                    "description": "Share of a vintage's loans that must be resolved for it to "
                                                   "count as seasoned, as a fraction. Default 0.8."},
             **MIN_SCORE_PARAM, **COHORT_PARAMS, **MIN_OUTCOMES_PARAM,
         }),
         vintage_trend_tool),

    Tool("loss_concentration",
         "Show where historical unrecovered principal is concentrated: each segment's share of losses versus its "
         "share of funded principal (complete terminal loans), the largest contributors and the segments that "
         "carry more or less than a proportional share. Segment by score_band, issue_year, grade or term.",
         object_schema({
             "dimension": {"type": "string", "enum": list(TITLES), "description": "How to segment the loans."},
             "cutoffs": CUTOFF_PARAMS["cutoffs"],
             **MIN_SCORE_PARAM, **COHORT_PARAMS, **MIN_OUTCOMES_PARAM,
         }, required=["dimension"]),
         loss_concentration_tool),

    Tool("mix_shift",
         "Show how the mix of issued loans changes by issue year: each year's share of loans by score_band, grade "
         "or term, the loan-weighted average score, and the change in each share between the first and last "
         "issue year in percentage points. Counts all issued loans, so seasoning does not affect it.",
         object_schema({
             "dimension": {"type": "string", "enum": list(MIX_DIMENSIONS),
                           "description": "Which mix to follow across issue years."},
             "cutoffs": CUTOFF_PARAMS["cutoffs"],
             **COHORT_PARAMS,
         }, required=["dimension"]),
         mix_shift_tool),
)


def build_registry():
    """Create the registry shared by report steps, the model and the `call` command."""
    return ToolRegistry(TOOLS)
