"""Data overview: snapshot provenance, source coverage and cohort exclusions."""

from ..cube import ACCEPTED, REJECTED, SOURCES
from ..formatting import table
from ..tools import Tool, ToolOutput, object_schema
from .common import COHORT_PARAMS, quality, scenario_for


def analyze(snapshot, scenario):
    sources = {}
    for kind in SOURCES:
        cube = snapshot.cubes[kind]
        first, last = cube.month_bounds()
        sources[kind] = {"table": snapshot.sources[kind]["table"], "total_rows": cube.total_rows(),
                         "first_month": first, "last_month": last,
                         "dimensions": list(snapshot.sources[kind]["dimensions"])}
    return {
        "snapshot": {"snapshot_id": snapshot.snapshot_id, "database": snapshot.database,
                     "read_started_at_utc": snapshot.read_started_at_utc,
                     "read_completed_at_utc": snapshot.read_completed_at_utc},
        "cohort": {"start_month": scenario.start_month, "end_month": scenario.end_month},
        "sources": sources,
        "quality": {kind: quality(snapshot.cubes[kind].bands(scenario.cutoffs, scenario.start_month,
                                                             scenario.end_month)) for kind in SOURCES},
    }


def facts(result, scenario):
    snapshot = result["snapshot"]
    facts = {"snapshot": (
        f"Snapshot {snapshot['snapshot_id']} was read from MySQL schema {snapshot['database']} between "
        f"{snapshot['read_started_at_utc']} and {snapshot['read_completed_at_utc']} in one read-only "
        "transaction. Read time is not the repayment observation date."
    )}
    for kind, months in ((ACCEPTED, "issue"), (REJECTED, "application")):
        source = result["sources"][kind]
        segments = f"; optional segments: {', '.join(source['dimensions']) or 'none'}" if kind == ACCEPTED else ""
        facts[f"source_{kind}"] = (
            f"{kind.capitalize()} source {snapshot['database']}.{source['table']}: {source['total_rows']:,} rows; "
            f"parseable {months} months {source['first_month'] or 'unavailable'} to "
            f"{source['last_month'] or 'unavailable'}{segments}."
        )
    for kind, quality in result["quality"].items():
        facts[f"quality_{kind}"] = (
            f"{kind}: {quality['total_rows']:,} table rows; {quality['in_scope_rows']:,} in cohort; "
            f"{quality['invalid_date_rows']:,} invalid dates; {quality['outside_period_rows']:,} outside cohort; "
            f"{quality['invalid_score_rows']:,} invalid scores and {quality['invalid_amount_rows']:,} invalid "
            f"amounts within cohort. Observed bounds: {quality['first_month']} to {quality['last_month']}."
        )
    rejected = result["quality"][REJECTED]
    if rejected["in_scope_rows"]:
        facts["quality_comparison"] = (
            f"Rejected invalid-score exclusions are {rejected['invalid_score_rows'] / rejected['in_scope_rows']:.4%} "
            f"of cohort rows; invalid-amount exclusions are "
            f"{rejected['invalid_amount_rows'] / rejected['in_scope_rows']:.4%}. "
            "Judge each issue by its own fraction; they may overlap and must not be added. "
            "Invalid scores include missing, malformed, noninteger and out-of-range values; "
            "the aggregates do not distinguish these reasons or prove the source scores are erroneous."
        )
    else:
        facts["quality_comparison"] = "No rejected rows are in scope; exclusion shares are unavailable."
    return facts


def coverage_section(result):
    rows = [[kind, f"{q['total_rows']:,}", f"{q['in_scope_rows']:,}", f"{q['invalid_date_rows']:,}",
             f"{q['outside_period_rows']:,}", f"{q['invalid_score_rows']:,}", f"{q['invalid_amount_rows']:,}"]
            for kind, q in result["quality"].items()]
    lines = [table(["Source", "Table rows", "In cohort", "Invalid date", "Outside cohort",
                    "Invalid score in cohort", "Invalid amount in cohort"], rows, ["---", *["---:"] * 6]), ""]
    for kind, q in result["quality"].items():
        lines.append(f"{kind.capitalize()} observed cohort bounds: {q['first_month'] or 'unavailable'} "
                     f"to {q['last_month'] or 'unavailable'}.")
    return "\n".join(lines)


def _describe(context, **arguments):
    scenario = scenario_for(context, arguments)
    result = analyze(context.snapshot, scenario)
    found = facts(result, scenario)
    return ToolOutput(f"Data overview, cohort {scenario.cohort}", result, found, tuple(found))


TOOLS = (
    Tool("describe_data",
         "Describe the loaded MySQL snapshot: source tables, row counts, parseable month ranges, optional "
         "segment columns, and exclusions (invalid dates, scores and amounts) for a cohort. Fast; use it "
         "for data-quality questions or to learn which months and segments exist.",
         object_schema(COHORT_PARAMS), _describe),
)
