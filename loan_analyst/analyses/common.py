"""Calculation rules and tool parameter schemas shared by the analysis plug-ins."""

import math
import re

from ..settings import MONTH_PATTERN

SUM_FIELDS = ("rows", "amount_rows", "amount_sum", "terminal_rows", "charged_off_rows",
              "performance_rows", "performance_principal", "loss_amount")
SCENARIO_KEYS = ("cutoffs", "baseline", "start_month", "end_month", "max_loss_rate", "min_outcomes")


def ratio(numerator, denominator):
    """Return an unavailable rate (None) when there is no denominator."""
    return numerator / denominator if denominator else None


def total(rows):
    """Add counts exactly and money with correctly rounded sums. Absent fields (rejected
    performance) are structural zeros, not measured outcomes."""
    totals = {}
    for field in SUM_FIELDS:
        values = (row.get(field, 0) for row in rows)
        if field.endswith("rows"):
            totals[field] = sum(values)
        else:
            totals[field] = math.fsum(values)
    return totals


def performance(totals, min_outcomes):
    """Calculate rates using the population appropriate to each measure.

    Terminal loans are Fully Paid or Charged Off. The loss proxy uses only
    terminal loans with complete financial data; its denominator is principal,
    not a loan count. Too few outcomes makes a rate unavailable, not zero.
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
    dates = [row[key] for row in scoped for key in ("first_month", "last_month") if row.get(key)]
    return {
        "total_rows": sum(row["rows"] for row in bands),
        "in_scope_rows": sum(row["rows"] for row in scoped),
        "invalid_date_rows": sum(row["rows"] for row in bands if row["scope"] == "invalid_date"),
        "outside_period_rows": sum(row["rows"] for row in bands if row["scope"] == "outside_period"),
        "invalid_score_rows": sum(row["rows"] for row in scoped if row["bucket"] == -1),
        "invalid_amount_rows": sum(row["rows"] - row["amount_rows"] for row in scoped),
        "first_month": min(dates) if dates else None, "last_month": max(dates) if dates else None,
    }


def select_candidate(scenarios, ceiling):
    """Most retained accepted loans among cutoffs meeting the ceiling with complete data."""
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
    return max(eligible, key=lambda row: (row["accepted"]["rows"], row["cutoff"]), default=None)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "value"


def scenario_for(context, arguments):
    """Apply validated tool arguments to the session scenario; cross-field rules rerun."""
    return context.scenario.with_overrides(**{key: arguments[key] for key in SCENARIO_KEYS if key in arguments})


# Tool parameters shared by analyses. Omitted or null arguments use the session's settings.
_MONTH = {"type": "string", "pattern": f"^{MONTH_PATTERN}$", "maxLength": 7}
COHORT_PARAMS = {
    "start_month": {**_MONTH, "description": "Inclusive YYYY-MM cohort start (loan issue month, rejected "
                                             "application month). Omit to keep the session cohort."},
    "end_month": {**_MONTH, "description": "Inclusive YYYY-MM cohort end. Omit to keep the session cohort."},
}
CUTOFF_PARAMS = {
    "cutoffs": {"type": "array", "items": {"type": "integer", "minimum": 300, "maximum": 850},
                "minItems": 1, "maxItems": 30,
                "description": "Inclusive minimum scores to compare; the baseline is added automatically."},
    "baseline": {"type": "integer", "minimum": 300, "maximum": 850,
                 "description": "Comparison cutoff; an illustrative reference, not a verified policy."},
}
MIN_OUTCOMES_PARAM = {"min_outcomes": {
    "type": "integer", "minimum": 1,
    "description": "Minimum relevant terminal outcomes before a rate is shown; a reporting threshold."}}
LOSS_CEILING_PARAM = {"max_loss_rate": {
    "type": "number", "minimum": 0, "maximum": 1,
    "description": "Historical loss ceiling as a fraction (0.08 means 8%) used to pick a screening "
                   "candidate. Omit to keep the session ceiling, if any."}}
