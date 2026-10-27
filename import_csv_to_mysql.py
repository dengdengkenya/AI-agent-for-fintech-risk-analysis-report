"""Stream accepted and rejected Lending Club CSVs into separate MySQL tables.

Run: .venv/Scripts/python.exe import_csv_to_mysql.py
Defaults: full files under data/, database demo, tables accepted and rejected.
Use --rows 100 for a small import or --help for sample-file and connection options.
"""

import argparse
from contextlib import closing
import csv
from getpass import getpass
import gzip
from itertools import islice
import os
from pathlib import Path
import sys
from time import monotonic


PROJECT_ROOT = Path(__file__).resolve().parent


def default_source(name: str) -> Path:
    """Prefer the extracted CSV; fall back to streaming its gzip archive."""
    filename = f"{name}_2007_to_2018Q4.csv"
    extracted = PROJECT_ROOT / "data" / f"{name}_2007_to_2018q4.csv" / filename
    compressed = PROJECT_ROOT / "data" / f"{filename}.gz"
    return extracted if extracted.is_file() else compressed


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def quote_identifier(name: str) -> str:
    """Quote identifiers, including rejected headers with spaces and hyphens."""
    if (
        not name or len(name) > 64 or name.endswith(" ")
        or "\x00" in name or any(ord(char) > 0xFFFF for char in name)
    ):
        raise ValueError(f"Invalid MySQL identifier: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def open_csv(path: Path):
    # Some accepted-loan descriptions exceed csv's default field-size limit.
    csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    return opener(path, "rt", encoding="utf-8-sig", newline="")


def read_columns(path: Path) -> list[str]:
    with open_csv(path) as source:
        columns = next(csv.reader(source, strict=True), [])
    if not columns or len({column.casefold() for column in columns}) != len(columns):
        raise ValueError(f"{path.name}: missing or duplicate column names")
    for column in columns:
        quote_identifier(column)
    return columns


def iter_rows(path: Path, columns: list[str]):
    """Preserve text and map empty fields to NULL; skip only blanks and footers."""
    with open_csv(path) as source:
        reader = csv.reader(source, strict=True)
        if next(reader, []) != columns:
            raise ValueError(f"{path.name}: header changed since validation")
        for record_number, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            if (
                columns[0] == "id"
                and row[0].startswith("Total amount funded in policy code ")
                and not any(row[1:])
            ):
                continue
            if len(row) != len(columns):
                raise ValueError(
                    f"{path.name}: record {record_number} has {len(row)} fields; "
                    f"expected {len(columns)}"
                )
            yield tuple(None if value == "" else value for value in row)


def iter_batches(rows, batch_size: int, max_bytes: int):
    """Bound each INSERT by row count and a conservative escaped UTF-8 size."""
    batch = []
    batch_bytes = 0
    for row in rows:
        # Escaping can double every source byte; allow quotes, commas and NULLs.
        row_bytes = 3 + sum(
            5 if value is None else 2 * len(value.encode("utf-8")) + 3
            for value in row
        )
        if row_bytes > max_bytes:
            raise ValueError(
                "A CSV row exceeds the safe INSERT size. Increase MySQL's "
                "max_allowed_packet and retry with new, empty destination tables."
            )
        if batch and batch_bytes + row_bytes > max_bytes:
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(row)
        batch_bytes += row_bytes
        if len(batch) >= batch_size:
            yield batch
            batch = []
            batch_bytes = 0
    if batch:
        yield batch


def prepare_tables(connection, database: str, sources: list, append: bool) -> None:
    """Check both destinations before inserting; never delete existing data."""
    database_sql = quote_identifier(database)
    with closing(connection.cursor(buffered=True)) as cursor:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS {database_sql} CHARACTER SET utf8mb4"
        )
        missing = []
        for table, _, columns in sources:
            table_sql = f"{database_sql}.{quote_identifier(table)}"
            cursor.execute(
                "SELECT TABLE_TYPE, ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (database, table),
            )
            existing = cursor.fetchone()
            if existing is None:
                missing.append((table_sql, columns))
                continue
            if existing != ("BASE TABLE", "InnoDB"):
                raise ValueError(f"{table_sql} must be an InnoDB table")
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_SET_NAME, EXTRA "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (database, table),
            )
            expected = [(column, "longtext", "YES", "utf8mb4", "") for column in columns]
            if cursor.fetchall() != expected:
                raise ValueError(
                    f"{table_sql} has an incompatible schema. Choose a new table name; "
                    "the importer uses nullable utf8mb4 LONGTEXT columns in CSV order."
                )
            cursor.execute(f"SELECT 1 FROM {table_sql} LIMIT 1")
            if cursor.fetchone() is not None and not append:
                raise ValueError(
                    f"{table_sql} already contains data. Choose new table names, "
                    "or use --append to intentionally add rows (duplicates are possible)."
                )
        for table_sql, columns in missing:
            definitions = ", ".join(
                f"{quote_identifier(column)} LONGTEXT NULL" for column in columns
            )
            cursor.execute(
                f"CREATE TABLE {table_sql} ({definitions}) "
                "ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4"
            )
    connection.commit()


def import_csv(connection, database, table, path, columns, batch_size, row_limit):
    table_sql = f"{quote_identifier(database)}.{quote_identifier(table)}"
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT INTO {table_sql} ({column_sql}) VALUES ({placeholders})"
    committed = 0
    last_report = monotonic()
    print(f"Loading {path.name} -> {database}.{table} ...", flush=True)
    with closing(connection.cursor()) as cursor:
        cursor.execute("SELECT @@max_allowed_packet")
        # Leave room for the SQL statement and protocol overhead.
        max_bytes = int(cursor.fetchone()[0]) - len(insert_sql.encode("utf-8")) - 1024
        if max_bytes < 1:
            raise ValueError("MySQL max_allowed_packet is too small for this table")
        try:
            with closing(iter_rows(path, columns)) as rows:
                for batch in iter_batches(islice(rows, row_limit), batch_size, max_bytes):
                    cursor.executemany(insert_sql, batch)
                    connection.commit()
                    committed += len(batch)
                    if monotonic() - last_report >= 5:
                        print(f"  {database}.{table}: {committed:,} rows committed", flush=True)
                        last_report = monotonic()
        except (Exception, KeyboardInterrupt):
            print(
                f"{database}.{table}: {committed:,} rows were confirmed committed. "
                "Earlier batches remain; this import does not resume automatically.",
                file=sys.stderr,
                flush=True,
            )
            # A disconnect during commit can leave the last batch's outcome unknown.
            # Do not hide the original failure if rollback also loses the connection.
            try:
                connection.rollback()
            except Exception:
                pass
            raise
    print(f"Loaded {committed:,} rows into {database}.{table}.", flush=True)
    return committed


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--database", "--schema", default="demo")
    parser.add_argument("--accepted-table", default="accepted")
    parser.add_argument("--rejected-table", default="rejected")
    parser.add_argument("--accepted-csv", type=project_path, default=default_source("accepted"))
    parser.add_argument("--rejected-csv", type=project_path, default=default_source("rejected"))
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per commit (default: 1000).")
    parser.add_argument("--rows", type=int, help="Maximum data rows per file (default: all).")
    parser.add_argument("--append", action="store_true", help="Allow adding rows to nonempty tables.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate CSVs and count rows without connecting to MySQL; respects --rows.",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.rows is not None and args.rows < 1:
        parser.error("--rows must be at least 1; omit it to import all rows")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.accepted_table.casefold() == args.rejected_table.casefold():
        parser.error("accepted and rejected table names must be different")

    try:
        for name in (args.database, args.accepted_table, args.rejected_table):
            quote_identifier(name)
        sources = [
            (table, path, read_columns(path))
            for table, path in (
                (args.accepted_table, args.accepted_csv),
                (args.rejected_table, args.rejected_csv),
            )
        ]
        for table, path, columns in sources:
            print(f"{path} -> {args.database}.{table} ({len(columns)} columns)", flush=True)
        if args.dry_run:
            for table, path, columns in sources:
                with closing(iter_rows(path, columns)) as rows:
                    count = sum(1 for _ in islice(rows, args.rows))
                print(f"Validated {count:,} data rows for {args.database}.{table}.", flush=True)
            return
    except (OSError, ValueError, csv.Error, EOFError) as error:
        parser.exit(1, f"CSV validation failed: {error}\n")

    try:
        import mysql.connector
    except ImportError:
        parser.exit(1, "MySQL connector is missing. Run: python -m pip install -r requirements.txt\n")

    connection = None
    try:
        password = os.environ.get("MYSQL_PASSWORD")
        if password is None:
            password = getpass(f"MySQL password for {args.user}@{args.host}: ")
        connection = mysql.connector.connect(
            host=args.host, port=args.port, user=args.user, password=password,
            charset="utf8mb4", connection_timeout=10, autocommit=False,
            sql_mode="TRADITIONAL",
        )
        prepare_tables(connection, args.database, sources, args.append)
        for table, path, columns in sources:
            import_csv(
                connection, args.database, table, path, columns, args.batch_size, args.rows
            )
    except (OSError, ValueError, csv.Error, mysql.connector.Error, EOFError) as error:
        parser.exit(1, f"Import failed: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Import interrupted. Previously committed batches remain.\n")
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    main()
