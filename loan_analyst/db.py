"""MySQL access: local credentials, connections, read-only transactions and table metadata.

This layer knows nothing about loans. Callers supply validated identifiers and SQL.
"""

from contextlib import closing, contextmanager
from getpass import getpass
import os
import sys


class ConfigurationError(RuntimeError):
    """Required host configuration or credentials are missing."""


def quote_identifier(name):
    """Quote identifiers, including rejected-table headers with spaces and hyphens."""
    if (not isinstance(name, str) or not name or len(name) > 64 or name.endswith(" ")
            or "\x00" in name or any(ord(char) > 0xFFFF for char in name)):
        raise ValueError(f"Invalid MySQL identifier: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def numeric_sql(column):
    """Validate text before casting, so blanks or bad text cannot silently become zero."""
    col = f"TRIM({quote_identifier(column)})"
    return (f"CASE WHEN {col} REGEXP '^[0-9]{{1,12}}([.][0-9]{{1,4}})?$' "
            f"THEN CAST({col} AS DECIMAL(20,4)) END")


def configure_query_timeout(connection, seconds):
    """Set a server-enforced per-SELECT time limit on this connection."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        raise ValueError("query_timeout must be a positive integer")
    with closing(connection.cursor()) as cursor:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (seconds * 1000,))


def connect_mysql(config, password):
    """Open and configure a connection; callers own and close successful results."""
    try:
        import mysql.connector
    except ImportError as error:
        raise ConfigurationError("Install the project requirements before connecting to MySQL") from error
    connection = mysql.connector.connect(
        host=config.host, port=config.port, user=config.user, password=password,
        charset="utf8mb4", connection_timeout=10, autocommit=True, sql_mode="TRADITIONAL",
    )
    try:
        configure_query_timeout(connection, config.query_timeout)
    except BaseException:
        connection.close()
        raise
    return connection


def password_connection_factory(config):
    """Supply credentials locally; never put them in arguments, model context or evidence."""
    password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise ConfigurationError(
                "Set MYSQL_PASSWORD locally or use an interactive terminal for the password prompt"
            )
        password = getpass(f"MySQL password for {config.user}@{config.host}: ")
    return connect_mysql(config, password)


@contextmanager
def read_only_snapshot(connection):
    """Share one read-only repeatable snapshot and always end it by rollback."""
    connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True,
                                 consistent_snapshot=True)
    try:
        with closing(connection.cursor(dictionary=True)) as cursor:
            yield cursor
    finally:
        connection.rollback()


def table_metadata(cursor, database, table):
    """Return (engine or None, column names) from metadata only; no records are scanned."""
    cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table))
    metadata = cursor.fetchone()
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table))
    return (metadata["ENGINE"] if metadata else None), {row["COLUMN_NAME"] for row in cursor.fetchall()}
