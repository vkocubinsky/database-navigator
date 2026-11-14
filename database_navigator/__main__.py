"""Temporary JSON command-line interface for inspecting backend results."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .postgres import postgres_foreign_keys, connect_read_only, read_operation, PostgreSQLError
from .sqlite import sqlite_foreign_keys
from .models import TableNotFoundError


def inspect(connection_string: str, table: str, schema: str | None = None):
    if connection_string.startswith("sqlite:///"):
        path = connection_string[len("sqlite:///"):]
        if not path:
            raise ValueError("SQLite connection string must contain a path")
        uri = path if path == ":memory:" else Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            return sqlite_foreign_keys(connection, table, schema or "main")
        finally:
            connection.close()
    if connection_string.startswith("sqlite:"):
        raise ValueError("Use sqlite:///path/to/database for SQLite")
    connection = connect_read_only(connection_string)
    try:
        with read_operation(connection):
            return postgres_foreign_keys(connection, table, schema)
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Inspect outgoing foreign keys (JSON)")
    parser.add_argument('--version', action='version', version=f'Database Navigator {__version__}')
    parser.add_argument("connection", help="SQLite URL or PostgreSQL libpq connection string")
    parser.add_argument("table", help="Literal table name; use --schema for the schema")
    parser.add_argument("--schema")
    args = parser.parse_args()
    try:
        keys = inspect(args.connection, args.table, args.schema)
    except (ValueError, TableNotFoundError, sqlite3.Error, PostgreSQLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps([asdict(key) for key in keys], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
