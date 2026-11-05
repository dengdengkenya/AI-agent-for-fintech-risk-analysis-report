"""Disjoint segment breakdowns of accepted-loan performance: score band, vintage, grade, term."""

from ..cube import ACCEPTED, bucket
from ..formatting import pct, table, usd
from ..tools import Tool, ToolOutput, ToolUnavailable, object_schema
from .common import (COHORT_PARAMS, CUTOFF_PARAMS, MIN_OUTCOMES_PARAM, performance, ratio, scenario_for, slug,
                     total)

TITLES = {"score_band": "Score band", "issue_year": "Issue year", "grade": "Grade", "term": "Term"}
MISSING = "invalid/missing"


def band_label(band, cutoffs):
    if band == -1:
        return "invalid score"
    if band == 0:
        return f"below {cutoffs[0]}"
    index = cutoffs.index(band)
    return f"{band}+" if index == len(cutoffs) - 1 else f"{band}-{cutoffs[index + 1] - 1}"


def _segment_key(row, dimension, cutoffs):
    """Choose the group that receives this aggregate row."""
    if dimension == "score_band":
        return bucket(row["score"], cutoffs)
    if dimension == "issue_year":
        return row["period"][:4]
    return row[dimension] or MISSING


def _segment_label(key, dimension, cutoffs):
    if dimension == "score_band":
        return band_label(key, cutoffs)
    if dimension == "term" and key != MISSING:
        return f"{key} months"
    return str(key)


def _segment_order(key, dimension):
    """Sort ordinary values first and invalid/missing values last."""
    if dimension == "score_band":
        return (key == -1, key)
    if dimension == "issue_year":
        return str(key)
    return (key == MISSING, key)


def analyze(snapshot, scenario, dimension, min_score=None):
    """Filter the cohort, group its rows, then calculate each group's share and rates."""
    cube = snapshot.cubes[ACCEPTED]
    if dimension not in TITLES:
        raise ValueError(f"Unknown dimension: {dimension}")
    if dimension in ("grade", "term") and dimension not in cube.dimensions:
        raise ToolUnavailable(f"{dimension} is unavailable: the accepted table has no {dimension} column")
    rows = cube.select(scenario.start_month, scenario.end_month, min_score=min_score)
    groups = {}
    for row in rows:
        key = _segment_key(row, dimension, scenario.cutoffs)
        groups.setdefault(key, []).append(row)

    loans = sum(row["rows"] for row in rows)
    segments = []
    for group in sorted(groups, key=lambda key: _segment_order(key, dimension)):
        totals = total(groups[group])
        segments.append({
            "segment": _segment_label(group, dimension, scenario.cutoffs),
            "accepted": totals,
            "loan_share": ratio(totals["rows"], loans),
            **performance(totals, scenario.min_outcomes),
        })
    return {"dimension": dimension, "min_score": min_score, "cohort": scenario.cohort,
            "min_outcomes": scenario.min_outcomes, "loans": loans, "segments": segments}


def facts(result):
    name = TITLES[result["dimension"]]
    scope = ("all loans in the cohort" if result["min_score"] is None else
             f"loans with a valid score at or above {result['min_score']}")
    facts = {"scope": (
        f"{name} breakdown of historical accepted loans: cohort {result['cohort']}; {scope}; {result['loans']:,} "
        f"loans. Rates need at least {result['min_outcomes']} relevant outcomes. Segments are descriptive "
        "historical groups, not causal effects."
        + (" Each score band holds only loans between adjacent cutoffs, so it shows the loans each cutoff step "
           "removes." if result["dimension"] == "score_band" else "")
    )}
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
            f"{pct(low['loss_rate'])}. This describes historical groups, not causes.")
    shares = [segment for segment in result["segments"] if segment["terminal_share"] is not None]
    if result["dimension"] == "issue_year" and len(shares) >= 2:
        low = min(shares, key=lambda segment: segment["terminal_share"])
        high = max(shares, key=lambda segment: segment["terminal_share"])
        facts["seasoning"] = (
            f"Terminal share ranges from {pct(low['terminal_share'])} (issue year {low['segment']}) to "
            f"{pct(high['terminal_share'])} (issue year {high['segment']}). Issue years with a low terminal share "
            "were mostly still open in the source data, so their loss proxies cover only resolved loans.")
    return facts


def highlights(result, found):
    if result["dimension"] == "score_band":
        return tuple(found)  # bands are few and drive the marginal cutoff tradeoff
    return tuple(key for key in ("scope", "loss_range", "seasoning") if key in found)


def section(result):
    rows = [[segment["segment"], f"{segment['accepted']['rows']:,}", pct(segment["loan_share"]),
             usd(segment["accepted"]["amount_sum"]), pct(segment["terminal_share"]), pct(segment["chargeoff_rate"]),
             pct(segment["loss_rate"]), pct(segment["loss_data_coverage"])] for segment in result["segments"]]
    return table([TITLES[result["dimension"]], "Loans", "Share", "Funded principal", "Terminal share",
                  "Charge-off rate", "Loss proxy", "Financial coverage"], rows, ["---", *["---:"] * 7])


def _segments(context, dimension, min_score=None, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze(context.snapshot, scenario, dimension, min_score)
    found = facts(result)
    title = f"{TITLES[dimension]} breakdown, cohort {scenario.cohort}"
    if min_score is not None:
        title += f", score {min_score}+"
    return ToolOutput(title, result, found, highlights(result, found))


TOOLS = (
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
             "cutoffs": CUTOFF_PARAMS["cutoffs"], **COHORT_PARAMS, **MIN_OUTCOMES_PARAM,
         }, required=["dimension"]), _segments),
)
