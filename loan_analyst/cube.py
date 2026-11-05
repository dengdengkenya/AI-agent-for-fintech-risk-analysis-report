"""Grouped aggregate cubes: one scan per source table serves every cohort and cutoff.

Each table is grouped by cohort month, score and optional segment columns. Only group
keys, counts and sums leave MySQL. Python derives cohorts, cutoff bands and rates from
them, so analyses and model tool calls never rescan or query the database.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path

from . import PROJECT_ROOT
from .artifacts import write_files
from .db import numeric_sql, quote_identifier, read_only_snapshot, table_metadata
from .jsonutil import read_json, sha256_json

ACCEPTED, REJECTED = "accepted", "rejected"
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
# The source dictionary: rejected Risk_Score is FICO before 2013-11-05, VantageScore after.
SCORE_SYSTEM_CHANGE = "2013-11"


class SchemaError(ValueError):
    """A source table is missing, empty or lacks required columns; safe to show."""


@dataclass(frozen=True)
class Dimension:
    """An optional validated grouping column; '{col}' marks the quoted source column."""

    name: str
    column: str
    expression: str

    def sql(self):
        return self.expression.replace("{col}", quote_identifier(self.column))


# Optional segments, grouped only when the source column exists. Add one here to extend.
DIMENSIONS = {
    ACCEPTED: (
        Dimension("grade", "grade", "CASE WHEN UPPER(TRIM({col})) REGEXP '^[A-G]$' THEN UPPER(TRIM({col})) END"),
        Dimension("term", "term", "CASE WHEN TRIM({col}) REGEXP '^(36|60) months$' THEN LEFT(TRIM({col}),2) END"),
    ),
    REJECTED: (),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_query(kind, config, dimensions=None):
    """Return one fixed grouped query for a source: no bound values and no borrower rows."""
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
        # Open loans do not have terminal outcomes. Missing financial values must
        # not become zero losses, so performance requires every financial field.
        terminal = "status IN ('Fully Paid','Charged Off')"
        valid = f"({terminal}) AND amount > 0 AND principal_received BETWEEN 0 AND amount AND recovered >= 0"
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
    parsed += "".join(f",\n        {dimension.sql()} AS {quote_identifier(dimension.name)}" for dimension in dimensions)
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
    """Highest cutoff met; 0 for a valid score below every cutoff; -1 for an invalid score."""
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


def sums(rows, metrics):
    """Exact counts and correctly rounded money sums (math.fsum)."""
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
        if any(set(row) != expected for row in self.rows):
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
                meets_cutoff = row["score"] is not None and row["score"] >= min_score
                if not meets_cutoff:
                    continue
            selected.append(row)
        return selected

    def bands(self, cutoffs, start_month=None, end_month=None):
        """Group by cohort membership and score band, keeping exclusions visible.

        Unlike select(), this includes out-of-cohort rows and invalid scores so
        quality reports can explain how many source loans were excluded.
        """
        groups = {}
        for row in self.rows:
            scope = classify(row["period"], start_month, end_month)
            band = bucket(row["score"], cutoffs)
            groups.setdefault((scope, band), []).append(row)
        bands = []
        for (scope, band), rows in sorted(groups.items()):
            periods = [row["period"] for row in rows if row["period"]]
            bands.append({"scope": scope, "bucket": band, **sums(rows, self.metrics),
                          "first_month": min(periods, default=None), "last_month": max(periods, default=None)})
        return bands

    def to_payload(self):
        columns = self.keys + self.metrics
        return {"columns": list(columns), "rows": [[row[column] for column in columns] for row in self.rows]}

    @classmethod
    def from_payload(cls, kind, dimensions, payload):
        columns = payload["columns"]
        return cls(kind, dimensions, [dict(zip(columns, values, strict=True)) for values in payload["rows"]])


def _plain(key, value):
    if isinstance(value, Decimal):
        return int(value) if key.endswith("rows") else float(value)
    return value


@dataclass(frozen=True)
class Snapshot:
    """A consistent read of both sources: cubes, provenance and a content hash.

    A cube is one table's grouped rows. A snapshot pairs both cubes with the
    source names, queries and read times needed to reproduce the report.
    """

    database: str
    sources: dict
    queries: dict
    cubes: dict
    read_started_at_utc: str
    read_completed_at_utc: str
    content_sha256: str = ""
    origin: str = "mysql"

    @classmethod
    def create(cls, database, sources, queries, cubes, started, completed, origin="mysql"):
        snapshot = cls(database, sources, queries, cubes, started, completed, "", origin)
        object.__setattr__(snapshot, "content_sha256", sha256_json(snapshot._content()))
        return snapshot

    def _content(self):
        return {"format": SNAPSHOT_FORMAT, "database": self.database, "sources": self.sources,
                "queries": self.queries, "read_started_at_utc": self.read_started_at_utc,
                "read_completed_at_utc": self.read_completed_at_utc,
                "cubes": {kind: cube.to_payload() for kind, cube in self.cubes.items()}}

    @property
    def snapshot_id(self):
        stamp = datetime.fromisoformat(self.read_completed_at_utc).astimezone(timezone.utc)
        return f"{stamp:%Y%m%dT%H%M%SZ}-{self.content_sha256[:8]}"

    def matches(self, config):
        return self.database == config.database and all(
            self.sources[kind]["table"] == getattr(config, f"{kind}_table") for kind in SOURCES)

    def provenance(self):
        """Summary for evidence files and reports, without the cube rows."""
        return {
            "snapshot_id": self.snapshot_id, "origin": self.origin, "database": self.database,
            "sources": {kind: {**self.sources[kind], "total_rows": self.cubes[kind].total_rows(),
                               "cube_rows": len(self.cubes[kind].rows)} for kind in SOURCES},
            "read_started_at_utc": self.read_started_at_utc,
            "read_completed_at_utc": self.read_completed_at_utc, "content_sha256": self.content_sha256,
        }

    def to_payload(self):
        return {**self._content(), "snapshot_id": self.snapshot_id, "content_sha256": self.content_sha256}

    @classmethod
    def from_payload(cls, payload, origin):
        try:
            if payload.get("format") != SNAPSHOT_FORMAT:
                raise ValueError
            cubes = {}
            for kind in SOURCES:
                dimensions = payload["sources"][kind]["dimensions"]
                cubes[kind] = Cube.from_payload(kind, dimensions, payload["cubes"][kind])
            snapshot = cls.create(payload["database"], payload["sources"], payload["queries"], cubes,
                                  payload["read_started_at_utc"], payload["read_completed_at_utc"], origin)
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
    return {
        "table": table,
        "engine": engine,
        "dimensions": [item.name for item in DIMENSIONS[kind] if item.column in columns],
    }


def read_snapshot(connection, config, clock=utc_now):
    """Check both schemas, then scan each source once inside one read-only snapshot."""
    started = clock()
    sources = {}
    queries = {}
    cubes = {}
    with read_only_snapshot(connection) as cursor:
        # Finish both cheap metadata checks before starting either expensive scan.
        for kind in SOURCES:
            sources[kind] = _source_metadata(cursor, config, kind)

        for kind in SOURCES:
            dimensions = [item for item in DIMENSIONS[kind] if item.name in sources[kind]["dimensions"]]
            queries[kind] = build_query(kind, config, dimensions)
            cursor.execute(queries[kind])
            rows = [{key: _plain(key, value) for key, value in row.items()} for row in cursor.fetchall()]
            if not rows:
                raise SchemaError(f"{kind} table is empty; import data before running the report")
            cubes[kind] = Cube(kind, sources[kind]["dimensions"], rows)
    return Snapshot.create(config.database, sources, queries, cubes, started, clock())


def display_path(path):
    """Project-relative path for evidence files, so shared reports carry no home folders."""
    path = Path(path).resolve()
    return path.relative_to(PROJECT_ROOT).as_posix() if path.is_relative_to(PROJECT_ROOT) else path.name


def save_snapshot(snapshot, directory):
    """Write the snapshot once under its unique ID and return the path."""
    path = Path(directory) / f"snapshot-{snapshot.snapshot_id}.json"
    if not path.exists():
        write_files(path.parent, {path.name: json.dumps(snapshot.to_payload(), separators=(",", ":"),
                                                        allow_nan=False)})
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
