"""Grouped MySQL data and the saved snapshot file.

A snapshot is one read of both source tables. MySQL does the grouping by month,
score and (when present) grade/term; Python receives only counts and sums. Every
report number is then calculated from this in-memory snapshot, so no analysis or
model tool can trigger another database query.

Read this file in three parts:
1. ``build_query`` - the exact SQL sent to MySQL.
2. ``Cube``        - grouped rows for one table, with cohort/score-band helpers.
3. ``Snapshot``    - both cubes plus provenance and a content hash.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path

from .database import begin_read_only_snapshot, numeric_sql, quote_identifier, table_metadata
from .support import display_path, read_json, sha256_json, utc_now, write_files

ACCEPTED = "accepted"
REJECTED = "rejected"
SOURCES = (ACCEPTED, REJECTED)

REQUIRED = {
    ACCEPTED: frozenset({"fico_range_low", "fico_range_high", "funded_amnt", "issue_d",
                         "loan_status", "total_rec_prncp", "recoveries"}),
    REJECTED: frozenset({"Risk_Score", "Amount Requested", "Application Date"}),
}

METRICS = {
    ACCEPTED: ("rows", "amount_rows", "amount_sum", "terminal_rows", "charged_off_rows",
               "performance_rows", "performance_principal", "loss_amount"),
    REJECTED: ("rows", "amount_rows", "amount_sum"),
}

SNAPSHOT_FORMAT = "loan-analyst-snapshot/1"

# The source dictionary says rejected Risk_Score changed from FICO to
# VantageScore in November 2013. Keep that changeover month separate.
SCORE_SYSTEM_CHANGE = "2013-11"


class SchemaError(ValueError):
    """A source table is missing, empty or lacks required columns; safe to show."""


@dataclass(frozen=True)
class Dimension:
    """An optional grouping column; '{col}' marks the quoted source column."""

    name: str
    column: str
    expression: str

    def sql(self):
        return self.expression.replace("{col}", quote_identifier(self.column))


# Optional segments, grouped only when the source column exists.
DIMENSIONS = {
    ACCEPTED: (
        Dimension("grade", "grade",
                  "CASE WHEN UPPER(TRIM({col})) REGEXP '^[A-G]$' THEN UPPER(TRIM({col})) END"),
        Dimension("term", "term",
                  "CASE WHEN TRIM({col}) REGEXP '^(36|60) months$' THEN LEFT(TRIM({col}),2) END"),
    ),
    REJECTED: (),
}


def build_query(kind, config, dimensions=None):
    """Return one fixed grouped query for a source: no bound values, no borrower rows.

    The query has two steps:
      parsed  - read the raw text columns and validate them into real numbers/dates
      scored  - turn the two FICO endpoints into one midpoint score
    It then groups by month, score and any optional segment, and sums the metrics.
    """
    if not isinstance(kind, str) or kind not in SOURCES:
        raise ValueError("kind must be 'accepted' or 'rejected'")

    dimensions = DIMENSIONS[kind] if dimensions is None else tuple(dimensions)
    table = getattr(config, f"{kind}_table")
    source = f"{quote_identifier(config.database)}.{quote_identifier(table)}"

    if kind == ACCEPTED:
        months = "'Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'"
        date_sql = (
            "CASE WHEN TRIM(`issue_d`) REGEXP '^[A-Za-z]{3}-[12][0-9]{3}$' "
            f"AND FIELD(LEFT(TRIM(`issue_d`),3),{months}) > 0 "
            f"THEN CONCAT(RIGHT(TRIM(`issue_d`),4),'-',LPAD(FIELD(LEFT(TRIM(`issue_d`),3),{months}),2,'0')) END"
        )
        parsed = f"""{numeric_sql('fico_range_low')} AS score_low,
        {numeric_sql('fico_range_high')} AS score_high,
        {numeric_sql('funded_amnt')} AS amount,
        {numeric_sql('total_rec_prncp')} AS principal_received,
        {numeric_sql('recoveries')} AS recovered,
        TRIM(`loan_status`) AS status, {date_sql} AS period"""
        score_sql = ("CASE WHEN score_low BETWEEN 300 AND 850 AND score_high BETWEEN 300 AND 850 "
                     "AND score_low <= score_high AND MOD(score_low,1)=0 AND MOD(score_high,1)=0 "
                     "THEN (score_low+score_high)/2 END")

        # A loan has a terminal outcome only if it is Fully Paid or Charged Off.
        # Financial metrics also require every money field to be present and valid,
        # so missing data is excluded rather than counted as a zero loss.
        terminal = "status IN ('Fully Paid','Charged Off')"
        valid = (f"({terminal}) AND amount > 0 AND principal_received BETWEEN 0 AND amount "
                 "AND recovered >= 0")
        # The five metrics below are cumulative sums, not loan rows:
        #   terminal_rows        resolved loans (the charge-off denominator)
        #   charged_off_rows     resolved loans that charged off
        #   performance_rows     resolved loans with complete financial fields
        #   performance_principal funded principal of those complete loans
        #   loss_amount          unrecovered principal on complete charged-off loans
        metrics = f""",
    SUM(CASE WHEN {terminal} THEN 1 ELSE 0 END) AS terminal_rows,
    SUM(CASE WHEN status='Charged Off' THEN 1 ELSE 0 END) AS charged_off_rows,
    SUM(CASE WHEN {valid} THEN 1 ELSE 0 END) AS performance_rows,
    SUM(CASE WHEN {valid} THEN amount ELSE 0 END) AS performance_principal,
    SUM(CASE WHEN {valid} AND status='Charged Off'
        THEN GREATEST(amount-principal_received-recovered,0) ELSE 0 END) AS loss_amount"""
    else:
        date_sql = """CASE WHEN TRIM(`Application Date`) REGEXP
            '^[12][0-9]{3}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
            THEN CASE WHEN CAST(RIGHT(TRIM(`Application Date`),2) AS UNSIGNED)
                <= DAY(LAST_DAY(CONCAT(LEFT(TRIM(`Application Date`),7),'-01')))
                THEN LEFT(TRIM(`Application Date`),7) END END"""
        parsed = f"""{numeric_sql('Risk_Score')} AS raw_score,
        {numeric_sql('Amount Requested')} AS amount, {date_sql} AS period"""
        score_sql = "CASE WHEN raw_score BETWEEN 300 AND 850 AND MOD(raw_score,1)=0 THEN raw_score END"
        metrics = ""

    for dimension in dimensions:
        parsed += f",\n        {dimension.sql()} AS {quote_identifier(dimension.name)}"

    keys = ", ".join(["period", "score", *(quote_identifier(dimension.name) for dimension in dimensions)])
    return f"""WITH parsed AS (
    SELECT {parsed} FROM {source}
), scored AS (
    SELECT parsed.*, {score_sql} AS score FROM parsed
)
SELECT {keys}, COUNT(*) AS `rows`,
    SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END) AS amount_rows,
    SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS amount_sum{metrics}
FROM scored GROUP BY {keys} ORDER BY {keys}"""


def bucket(score, cutoffs):
    """Return the highest cutoff a score meets, 0 if below all, -1 if invalid."""
    if score is None:
        return -1
    # Scenario validates and sorts cutoffs before any analysis runs.
    for cutoff in reversed(cutoffs):
        if score >= cutoff:
            return cutoff
    return 0


def classify(period, start_month=None, end_month=None):
    """Classify a parsed YYYY-MM month using inclusive cohort bounds."""
    if period is None:
        return "invalid_date"
    if (start_month and period < start_month) or (end_month and period > end_month):
        return "outside_period"
    return "in_scope"


def sum_metrics(rows, metrics):
    """Add counts exactly and money with correctly rounded sums (math.fsum)."""
    totals = {}
    for metric in metrics:
        values = (row[metric] for row in rows)
        if metric.endswith("rows"):
            totals[metric] = sum(values)
        else:
            totals[metric] = math.fsum(values)
    return totals


class Cube:
    """Grouped rows for one source: group keys plus additive count and sum metrics."""

    def __init__(self, kind, dimensions, rows):
        self.kind = kind
        self.dimensions = tuple(dimensions)
        self.metrics = METRICS[kind]
        self.keys = ("period", "score", *self.dimensions)
        self.rows = list(rows)

        expected = set(self.keys) | set(self.metrics)
        for row in self.rows:
            if set(row) != expected:
                raise ValueError(f"{kind}: unexpected aggregate columns")

    def total_rows(self):
        return sum(row["rows"] for row in self.rows)

    def month_bounds(self):
        periods = [row["period"] for row in self.rows if row["period"]]
        return min(periods, default=None), max(periods, default=None)

    def select(self, start_month=None, end_month=None, min_score=None):
        """In-cohort rows, optionally only those with a valid score at or above min_score."""
        selected = []
        for row in self.rows:
            if classify(row["period"], start_month, end_month) != "in_scope":
                continue
            if min_score is not None:
                has_score = row["score"] is not None and row["score"] >= min_score
                if not has_score:
                    continue
            selected.append(row)
        return selected

    def bands(self, cutoffs, start_month=None, end_month=None):
        """Group rows by cohort membership and score band, keeping exclusions visible.

        Unlike select(), this includes out-of-cohort rows and invalid scores so
        quality reports can explain how many source loans were excluded.
        """
        groups = {}
        for row in self.rows:
            scope = classify(row["period"], start_month, end_month)
            band = bucket(row["score"], cutoffs)
            groups.setdefault((scope, band), []).append(row)

        # Every source row lands in exactly one (scope, band) group, so these
        # bands can be added together without double counting.
        bands = []
        for (scope, band), rows in sorted(groups.items()):
            periods = [row["period"] for row in rows if row["period"]]
            bands.append({
                "scope": scope,
                "bucket": band,
                **sum_metrics(rows, self.metrics),
                "first_month": min(periods, default=None),
                "last_month": max(periods, default=None),
            })
        return bands

    def to_payload(self):
        columns = self.keys + self.metrics
        rows = []
        for row in self.rows:
            rows.append([row[column] for column in columns])
        return {"columns": list(columns), "rows": rows}

    @classmethod
    def from_payload(cls, kind, dimensions, payload):
        columns = payload["columns"]
        rows = []
        for values in payload["rows"]:
            if len(values) != len(columns):
                raise ValueError("aggregate row does not match its columns")
            rows.append(dict(zip(columns, values)))
        return cls(kind, dimensions, rows)


def _plain(key, value):
    """Convert Decimal values from MySQL into JSON-friendly ints and floats."""
    if isinstance(value, Decimal):
        return int(value) if key.endswith("rows") else float(value)
    return value


class Snapshot:
    """A consistent read of both sources: cubes, provenance and a content hash.

    A cube is one table's grouped rows. A snapshot pairs both cubes with the
    source names, SQL and read times needed to reproduce the report.
    """

    def __init__(self, database, sources, queries, cubes, read_started_at_utc,
                 read_completed_at_utc, origin="mysql"):
        self.database = database
        self.sources = sources
        self.queries = queries
        self.cubes = cubes
        self.read_started_at_utc = read_started_at_utc
        self.read_completed_at_utc = read_completed_at_utc
        self.origin = origin
        self.content_sha256 = sha256_json(self._content())

    def _content(self):
        cubes = {}
        for kind, cube in self.cubes.items():
            cubes[kind] = cube.to_payload()
        return {
            "format": SNAPSHOT_FORMAT,
            "database": self.database,
            "sources": self.sources,
            "queries": self.queries,
            "read_started_at_utc": self.read_started_at_utc,
            "read_completed_at_utc": self.read_completed_at_utc,
            "cubes": cubes,
        }

    @property
    def snapshot_id(self):
        stamp = datetime.fromisoformat(self.read_completed_at_utc).astimezone(timezone.utc)
        return f"{stamp:%Y%m%dT%H%M%SZ}-{self.content_sha256[:8]}"

    def matches(self, config):
        expected_tables = {
            ACCEPTED: config.accepted_table,
            REJECTED: config.rejected_table,
        }
        if self.database != config.database:
            return False
        for kind in SOURCES:
            if self.sources[kind]["table"] != expected_tables[kind]:
                return False
        return True

    def provenance(self):
        """Summary for evidence files and reports, without the cube rows."""
        sources = {}
        for kind in SOURCES:
            sources[kind] = {
                **self.sources[kind],
                "total_rows": self.cubes[kind].total_rows(),
                "cube_rows": len(self.cubes[kind].rows),
            }
        return {
            "snapshot_id": self.snapshot_id,
            "origin": self.origin,
            "database": self.database,
            "sources": sources,
            "read_started_at_utc": self.read_started_at_utc,
            "read_completed_at_utc": self.read_completed_at_utc,
            "content_sha256": self.content_sha256,
        }

    def to_payload(self):
        return {**self._content(), "snapshot_id": self.snapshot_id,
                "content_sha256": self.content_sha256}

    @classmethod
    def from_payload(cls, payload, origin):
        try:
            if payload.get("format") != SNAPSHOT_FORMAT:
                raise ValueError

            cubes = {}
            for kind in SOURCES:
                dimensions = payload["sources"][kind]["dimensions"]
                cubes[kind] = Cube.from_payload(kind, dimensions, payload["cubes"][kind])

            snapshot = cls(
                payload["database"],
                payload["sources"],
                payload["queries"],
                cubes,
                payload["read_started_at_utc"],
                payload["read_completed_at_utc"],
                origin,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError("Not a complete loan analyst snapshot file") from None

        if snapshot.content_sha256 != payload.get("content_sha256"):
            raise ValueError("Snapshot content does not match its recorded hash; it was changed or corrupted")
        return snapshot


def _source_metadata(cursor, config, kind):
    """Validate one table and record which optional grouping columns it provides."""
    table = getattr(config, f"{kind}_table")
    engine, columns = table_metadata(cursor, config.database, table)

    if engine != "InnoDB":
        raise SchemaError(f"{kind}: expected an existing InnoDB table")
    missing = sorted(REQUIRED[kind] - columns)
    if missing:
        raise SchemaError(f"{kind}: missing required columns: " + ", ".join(missing))

    dimensions = []
    for item in DIMENSIONS[kind]:
        if item.column in columns:
            dimensions.append(item.name)

    return {"table": table, "engine": engine, "dimensions": dimensions}


def read_snapshot(connection, config, clock=utc_now):
    """Check both schemas, then scan each source once inside one read-only snapshot."""
    started = clock()
    sources = {}
    queries = {}
    cubes = {}

    begin_read_only_snapshot(connection)
    try:
        cursor = connection.cursor(dictionary=True)
        try:
            # Cheap metadata checks first, before either expensive scan.
            for kind in SOURCES:
                sources[kind] = _source_metadata(cursor, config, kind)

            for kind in SOURCES:
                dimensions = [item for item in DIMENSIONS[kind]
                              if item.name in sources[kind]["dimensions"]]
                queries[kind] = build_query(kind, config, dimensions)
                cursor.execute(queries[kind])

                rows = []
                for row in cursor.fetchall():
                    rows.append({key: _plain(key, value) for key, value in row.items()})
                if not rows:
                    raise SchemaError(f"{kind} table is empty; import data before running the report")
                cubes[kind] = Cube(kind, sources[kind]["dimensions"], rows)
        finally:
            cursor.close()
    finally:
        connection.rollback()

    return Snapshot(config.database, sources, queries, cubes, started, clock())


def save_snapshot(snapshot, directory):
    """Write the snapshot once under its unique ID and return the path."""
    path = Path(directory) / f"snapshot-{snapshot.snapshot_id}.json"
    if not path.exists():
        contents = json.dumps(snapshot.to_payload(), separators=(",", ":"), allow_nan=False)
        write_files(path.parent, {path.name: contents})
    return path


def _read(path):
    try:
        payload = read_json(path.read_text(encoding="utf-8"))
    except OSError:
        raise ValueError(f"Cannot read snapshot file {display_path(path)}") from None
    except ValueError:
        raise ValueError(f"{display_path(path)} is not valid JSON") from None
    return Snapshot.from_payload(payload, origin=f"saved file {display_path(path)}")


def load_snapshot(reference, directory, config):
    """Load an explicit snapshot file, or the newest saved one for these tables ('latest')."""
    if reference == "latest":
        for path in sorted(Path(directory).glob("snapshot-*.json"), reverse=True):
            snapshot = _read(path)
            if snapshot.matches(config):
                return snapshot
        raise ValueError(f"No saved snapshot of {config.database}.{config.accepted_table} and "
                         f"{config.database}.{config.rejected_table}; run once without --snapshot")

    snapshot = _read(Path(reference))
    if not snapshot.matches(config):
        raise ValueError(f"{display_path(reference)} was read from other tables; pass matching "
                         "--database, --accepted-table and --rejected-table")
    return snapshot
