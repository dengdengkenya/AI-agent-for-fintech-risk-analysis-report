"""Append the first few Lending Club CSV rows to a MySQL table."""

import argparse
import csv
from getpass import getpass
from itertools import islice
import os
from pathlib import Path

import mysql.connector


PROJECT_ROOT = Path(__file__).resolve().parent
CSV_PATH = (
    PROJECT_ROOT
    / "data"
    / "accepted_2007_to_2018q4.csv"
    / "accepted_2007_to_2018Q4.csv"
)


def quote_identifier(name: str) -> str:
    """Quote a database, table, or column name; values use SQL parameters."""
    if not name or len(name) > 64 or "\x00" in name:
        raise ValueError(f"Invalid MySQL identifier:  ")
    return "`" + name.replace("`", "``") + "`"


def read_preview(row_count: int) -> tuple[list[str], list[list[str | None]]]:
    """Read only the header and requested rows, preserving CSV values as text."""
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source)
        columns = next(reader, [])
        if not columns or len(set(columns)) != len(columns):
            raise ValueError("The CSV must have a nonempty header with unique column names.")
        for column in columns:
            quote_identifier(column)

        rows = []
        for record_number, row in enumerate(islice(reader, row_count), start=2):
            if len(row) != len(columns):
                raise ValueError(f"CSV record {record_number} has an unexpected field count.")
            rows.append([None if value == "" else value for value in row])
        return columns, rows


def write_to_mysql(columns, rows, *, host, port, user, password, database, table) -> int:
    """Create the destination if needed and append rows in one transaction."""
    database_sql = quote_identifier(database)
    table_sql = f"{database_sql}.{quote_identifier(table)}"
    # This is a raw preview table: text avoids guessing types from a small sample.
    column_definitions = ", ".join(
        f"{quote_identifier(column)} LONGTEXT NULL" for column in columns
    )
    column_names = ", ".join(quote_identifier(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))

    connection = mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        charset="utf8mb4",
        connection_timeout=10,
        autocommit=False,
    )
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {database_sql} CHARACTER SET utf8mb4"
            )
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {table_sql} ({column_definitions}) "
                "ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4"
            )
            if rows:
                cursor.executemany(
                    f"INSERT INTO {table_sql} ({column_names}) VALUES ({placeholders})",
                    rows,
                )
            connection.commit()
        except Exception:
            # DDL is committed by MySQL separately; failed inserts are rolled back.
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()
    return len(rows)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5, help="Data rows to import (default: 5).")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--database", "--schema", default="demo", help="MySQL schema (default: demo).")
    parser.add_argument("--table", default="accepted_preview")
    args = parser.parse_args(argv)
    if args.rows < 0:
        parser.error("--rows must be zero or greater")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        columns, rows = read_preview(args.rows)
        password = os.environ.get("MYSQL_PASSWORD")
        if password is None:
            password = getpass(f"MySQL password for {args.user}@{args.host}: ")
        count = write_to_mysql(
            columns, rows, host=args.host, port=args.port, user=args.user,
            password=password, database=args.database, table=args.table,
        )
    except (OSError, ValueError, csv.Error, mysql.connector.Error, EOFError) as error:
        parser.exit(1, f"Import failed: {error}\n")
    print(f"Inserted {count} rows into {args.database}.{args.table} ({len(columns)} columns).")


if __name__ == "__main__":
    main()
