# Copyright (C) 2026 vkocubinsky
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Temporary JSON command-line interface for inspecting backend results."""

import json
from pathlib import Path
import sqlite3
import sys

from .connection import connection_parser, select_backend
from .db_postgres import postgres_foreign_keys, connect_read_only, read_operation, PostgreSQLError
from .db_sqlite import sqlite_foreign_keys
from .contracts import TableNotFoundError


__all__ = ["main"]


def inspect(connection_string: str, table: str, schema: str | None = None, *, backend=None):
    if select_backend(connection_string, backend) == "sqlite":
        path = connection_string[len("sqlite:///"):]
        if not path:
            raise ValueError("SQLite connection string must contain a path")
        uri = path if path == ":memory:" else Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            return sqlite_foreign_keys(connection, table, schema or "main")
        finally:
            connection.close()
    connection = connect_read_only(connection_string)
    try:
        with read_operation(connection):
            return postgres_foreign_keys(connection, table, schema)
    finally:
        connection.close()


def main():
    parser = connection_parser("List foreign keys that link this table to its parents (JSON)")
    parser.add_argument("table", help="Literal table name; use --schema for the schema")
    parser.add_argument("--schema")
    args = parser.parse_intermixed_args()
    try:
        keys = inspect(args.connection, args.table, args.schema, backend=args.backend)
    except (ValueError, TableNotFoundError, sqlite3.Error, PostgreSQLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps([{
        'name': key.name,
        'child_schema': key.child_table.schema, 'child_table': key.child_table.name,
        'child_key': key.child_key,
        'parent_schema': key.parent_table.schema, 'parent_table': key.parent_table.name,
        'parent_key': key.parent_key,
    } for key in keys], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
