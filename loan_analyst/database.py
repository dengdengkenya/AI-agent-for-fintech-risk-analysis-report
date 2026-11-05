"""Everything that talks to MySQL.

This file knows nothing about loans: callers give it validated table names and
SQL. The important safety rule lives here: a report reads both source tables
inside one read-only transaction, and that transaction is always rolled back.
"""

from getpass import getpass
import os
import sys


class ConfigurationError(RuntimeError):
    """Required host configuration or credentials are missing."""


def quote_identifier(name):
    """Put backticks around a MySQL name, including headers with spaces or hyphens."""
    if (not isinstance(name, str) or not name or len(name) > 64 or name.endswith(" ")
            or "\x00" in name or any(ord(char) > 0xFFFF for char in name)):
        raise ValueError(f"Invalid MySQL identifier: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def numeric_sql(column):
    """Validate text before casting it, so blanks or bad text never become zero."""
    quoted = f"TRIM({quote_identifier(column)})"
    return (f"CASE WHEN {quoted} REGEXP '^[0-9]{{1,12}}([.][0-9]{{1,4}})?$' "
            f"THEN CAST({quoted} AS DECIMAL(20,4)) END")


def configure_query_timeout(connection, seconds):
    """Ask MySQL to stop any single SELECT that runs longer than this."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        raise ValueError("query_timeout must be a positive integer")
    cursor = connection.cursor()
    try:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (seconds * 1000,))
    finally:
        cursor.close()


def connect_mysql(config, password):
    """Open one configured MySQL connection. The caller owns and closes it."""
    try:
        import mysql.connector
    except ImportError as error:
        raise ConfigurationError("Install the project requirements before connecting to MySQL") from error

    connection = mysql.connector.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=password,
        charset="utf8mb4",
        connection_timeout=10,
        autocommit=True,
        sql_mode="TRADITIONAL",
    )
    try:
        configure_query_timeout(connection, config.query_timeout)
    except BaseException:
        connection.close()
        raise
    return connection


def password_connection_factory(config):
    """Get the password locally, then connect.

    Passwords are never command-line arguments, config values, model context or
    evidence. They come from MYSQL_PASSWORD or an interactive prompt.
    """
    password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise ConfigurationError(
                "Set MYSQL_PASSWORD locally or use an interactive terminal for the password prompt")
        password = getpass(f"MySQL password for {config.user}@{config.host}: ")
    return connect_mysql(config, password)


def begin_read_only_snapshot(connection):
    """Start one repeatable, read-only transaction for all snapshot queries."""
    connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True,
                                 consistent_snapshot=True)


def table_metadata(cursor, database, table):
    """Return (engine, column names) from MySQL metadata without reading loan rows."""
    cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table))
    metadata = cursor.fetchone()

    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table))
    columns = set()
    for row in cursor.fetchall():
        columns.add(row["COLUMN_NAME"])
    return (metadata["ENGINE"] if metadata else None), columns
