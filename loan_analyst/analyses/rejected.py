"""Rejected applications: numeric score screens by score era or application year."""

from ..cube import REJECTED, SCORE_SYSTEM_CHANGE
from ..formatting import pct, table, usd
from ..tools import Tool, ToolOutput, object_schema
from .common import COHORT_PARAMS, CUTOFF_PARAMS, ratio, scenario_for, slug, total

ERAS = {
    "fico": f"FICO era (before {SCORE_SYSTEM_CHANGE})",
    "transition": f"{SCORE_SYSTEM_CHANGE} transition month (mixed score systems)",
    "vantage": f"VantageScore era (after {SCORE_SYSTEM_CHANGE})",
}
GROUPINGS = {"score_era": "score era", "application_year": "application year"}


def era(period):
    return "fico" if period < SCORE_SYSTEM_CHANGE else "transition" if period == SCORE_SYSTEM_CHANGE else "vantage"


def _screen(rows):
    values = total(rows)
    return {key: values[key] for key in ("rows", "amount_rows", "amount_sum")}


def analyze(snapshot, scenario, by="score_era"):
    rows = snapshot.cubes[REJECTED].select(scenario.start_month, scenario.end_month)
    key = (lambda row: era(row["period"])) if by == "score_era" else (lambda row: row["period"][:4])
    groups = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    order = list(ERAS) if by == "score_era" else sorted(groups)
    result = []
    for group in (group for group in order if group in groups):
        members = groups[group]
        usable = [row for row in members if row["score"] is not None]
        result.append({
            "group": ERAS[group] if by == "score_era" else group, **_screen(members),
            "usable_score_rows": sum(row["rows"] for row in usable),
            "screens": [{"cutoff": cutoff, **_screen([row for row in usable if row["score"] >= cutoff])}
                        for cutoff in scenario.cutoffs],
        })
    return {"by": by, "cohort": scenario.cohort, "baseline": scenario.baseline,
            "cutoffs": list(scenario.cutoffs), "groups": result}


def _at_baseline(group, baseline):
    return next(screen for screen in group["screens"] if screen["cutoff"] == baseline)


def facts(result):
    facts = {"scope": (
        f"Rejected applications grouped by {GROUPINGS[result['by']]}; cohort {result['cohort']}. A numeric screen "
        "counts applications whose Risk_Score is at or above a threshold. Risk_Score is FICO before "
        f"{SCORE_SYSTEM_CHANGE} and VantageScore after ({SCORE_SYSTEM_CHANGE} mixes both); it is not comparable to "
        "accepted FICO, and rejected applications have no repayment outcomes."
    )}
    for group in result["groups"]:
        screen = _at_baseline(group, result["baseline"])
        facts[slug(group["group"])] = (
            f"{group['group']}: {group['rows']:,} rejected applications in the cohort; {group['usable_score_rows']:,} "
            f"with a usable numeric score. At or above {result['baseline']}: {screen['rows']:,} applications "
            f"({pct(ratio(screen['rows'], group['usable_score_rows']))} of usable) with "
            f"{usd(screen['amount_sum'])} valid requested amount."
        )
    return facts


def section(result):
    baseline = result["baseline"]
    rows = []
    for group in result["groups"]:
        screen = _at_baseline(group, baseline)
        rows.append([group["group"], f"{group['rows']:,}", f"{group['usable_score_rows']:,}", f"{screen['rows']:,}",
                     pct(ratio(screen["rows"], group["usable_score_rows"])), usd(screen["amount_sum"])])
    return table([GROUPINGS[result["by"]].capitalize(), "Applications", "Usable score", f"At or above {baseline}",
                  "Share of usable", f"Requested amount at or above {baseline}"], rows, ["---", *["---:"] * 5])


def _rejected(context, by="score_era", **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze(context.snapshot, scenario, by)
    found = facts(result)
    return ToolOutput(f"Rejected applications by {GROUPINGS[by]}, cohort {scenario.cohort}", result, found,
                      tuple(found))


TOOLS = (
    Tool("rejected_applications",
         "Profile rejected applications: counts and valid requested amounts at or above each numeric score "
         "threshold, grouped by score era (FICO before 2013-11, VantageScore after) or by application year. "
         "Rejected scores are not comparable to accepted FICO and have no repayment outcomes.",
         object_schema({"by": {"type": "string", "enum": list(GROUPINGS), "description": "How to group applications."},
                        **CUTOFF_PARAMS, **COHORT_PARAMS}), _rejected),
)
