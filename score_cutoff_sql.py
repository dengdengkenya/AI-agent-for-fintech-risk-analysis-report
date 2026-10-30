"""Read-only MySQL operations for the score cutoff tool interface.

The original report remains an independent entry point. Its reviewed query
builder is reused here so both interfaces use the same aggregation rules.
"""

from contextlib import closing, contextmanager
from decimal import Decimal

from score_cutoff_simulation import REQUIRED, numeric_sql
from score_cutoff_simulation import build_query as _build_report_query


def _validate_kind(kind):
    if not isinstance(kind, str) or kind not in REQUIRED:
        raise ValueError("kind must be 'accepted' or 'rejected'")


def build_query(kind, args):
    """Return the existing parameterized aggregate query for a supported source."""
    _validate_kind(kind)
    return _build_report_query(kind, args)


def configure_query_timeout(connection, seconds):
    """Set a server-enforced per-SELECT time limit on this connection."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        raise ValueError("query_timeout must be a positive integer")
    with closing(connection.cursor()) as cursor:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (seconds * 1000,))


def connect_mysql(args, password):
    """Open and configure a connection; callers own and close successful results."""
    try:
        import mysql.connector
    except ImportError as error:
        raise RuntimeError("Install the project requirements before connecting to MySQL") from error
    connection = mysql.connector.connect(
        host=args.host, port=args.port, user=args.user, password=password,
        charset="utf8mb4", connection_timeout=10, autocommit=True, sql_mode="TRADITIONAL",
    )
    try:
        configure_query_timeout(connection, args.query_timeout)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def _snapshot_cursor(connection):
    """Share one read-only repeatable snapshot and always end it by rollback."""
    connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True,
                                 consistent_snapshot=True)
    try:
        with closing(connection.cursor(dictionary=True)) as cursor:
            yield cursor
    finally:
        connection.rollback()


def inspect_table(cursor, kind, args):
    """Read table metadata only; this does not scan application or loan records."""
    _validate_kind(kind)
    table = getattr(args, f"{kind}_table")
    cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (args.database, table))
    metadata = cursor.fetchone()
    engine = metadata["ENGINE"] if metadata else None
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (args.database, table))
    columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
    missing = sorted(REQUIRED[kind] - columns)
    return {
        "table": table,
        "engine": engine,
        "required_columns": sorted(REQUIRED[kind]),
        "missing_columns": missing,
        "valid": engine == "InnoDB" and not missing,
    }


def inspect_sources(connection, args):
    """Inspect both configured source schemas without querying their data."""
    with _snapshot_cursor(connection) as cursor:
        return {kind: inspect_table(cursor, kind, args) for kind in REQUIRED}


def _require_valid_source(kind, metadata):
    if metadata["engine"] != "InnoDB":
        raise ValueError(f"{kind}: expected an existing InnoDB table")
    if metadata["missing_columns"]:
        raise ValueError(f"{kind}: missing required columns: "
                         + ", ".join(metadata["missing_columns"]))


def fetch_table_aggregates(cursor, kind, args):
    """Execute one approved grouped query; return JSON-compatible aggregates."""
    sql, params = build_query(kind, args)
    cursor.execute(sql, params)
    rows = [
        {key: (int(value) if key.endswith("rows") else float(value))
         if isinstance(value, Decimal) else value for key, value in row.items()}
        for row in cursor.fetchall()
    ]
    if not rows:
        raise ValueError(f"{kind} table is empty; import data before running the report")
    return rows


def fetch_aggregates(connection, args):
    """Validate both schemas, then scan both sources in one read-only snapshot."""
    with _snapshot_cursor(connection) as cursor:
        for kind in REQUIRED:
            _require_valid_source(kind, inspect_table(cursor, kind, args))
        return {kind: fetch_table_aggregates(cursor, kind, args) for kind in REQUIRED}
