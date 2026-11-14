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

"""SQLite dialect, read-only backend, and metadata discovery."""

from dataclasses import replace
from pathlib import Path
import math
import re
import sqlite3
import time

from .models import ForeignKey, Relationship, TableNotFoundError, validate_limit
from .queries import (derived_reference, navigation_query, primary_key_values,
                      relationship_values, selected_sql)


_SQLITE_IDENTIFIER_FOLD = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def sqlite_identifier_key(name: str) -> str:
    """Match SQLite identifier equality: fold ASCII letters only."""
    return name.translate(_SQLITE_IDENTIFIER_FOLD)


def sqlite_quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SQLiteDialect:
    supports_schemas = False
    default_schema = 'main'

    def identifier_key(self, name):
        return sqlite_identifier_key(name)

    def validate_identifier(self, name):
        if not isinstance(name, str) or not name or '\0' in name:
            raise ValueError('Identifiers must be nonempty strings without NUL.')

    def validate_schema(self, schema):
        if schema != 'main':
            raise ValueError('The SQLite UI supports the main schema only.')

    def table_reference(self, table, schema=None):
        self.validate_schema(schema if schema is not None else self.default_schema)
        self.validate_identifier(table)
        # SQLite resolution accepts a literal table name, including dots/quotes.
        return table

    def split_reference(self, reference):
        return self.default_schema, reference

    def relationship_schema(self, schema):
        # The single supported schema is implicit in existing navigation labels.
        return None

    def validate_row_key(self, key, columns, row):
        return primary_key_values(key, columns, row, self.identifier_key)


SQLITE = SQLiteDialect()


# https://www.sqlite.org/lang_keywords.html — quote keywords and special names.
_SQLITE_KEYWORDS = frozenset('''
ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT
BEFORE BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT
CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH
ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST
FOLLOWING FOR FOREIGN FROM FULL GENERATED GLOB GROUP GROUPS HAVING IF IGNORE
IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT INTO IS
ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING
NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA
PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE
RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT SET
TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE
UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT
'''.split())


def sqlite_sql_identifier(name):
    """Keep ordinary SQL identifiers readable, quoting names that need protection."""
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', name) and name.upper() not in _SQLITE_KEYWORDS:
        return name
    return sqlite_quote_identifier(name)


def sqlite_literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, float) and math.isinf(value):
        # SQLite parses overflowing numeric literals as signed infinity.
        return '-1e999' if value < 0 else '1e999'
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    if isinstance(value, str):
        if '\0' in value:
            return f"CAST(X'{value.encode('utf-8').hex()}' AS TEXT)"
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"Cannot display a SQLite key of type {type(value).__name__}.")


def sqlite_relationship_identifiers(relationship):
    """Qualify columns so SQLite cannot interpret missing names as string literals."""
    table = sqlite_sql_identifier(relationship.table)
    return table, tuple(f'{table}.{sqlite_sql_identifier(name)}' for name in relationship.columns)


def derived_table(query):
    schema, table = derived_reference(query)
    if schema is not None:
        raise ValueError('Use an unqualified SQLite base-table name; override with -- derived: table_name if needed.')
    return table


def sqlite_primary_key(connection: sqlite3.Connection, table: str,
                       schema: str = 'main') -> tuple[str, ...]:
    """Return declared primary-key columns in key order, or () when absent."""
    cursor = connection.cursor()
    cursor.row_factory = None
    try:
        namespace = sqlite_quote_identifier(schema)
        found = cursor.execute(
            f'SELECT name FROM {namespace}.sqlite_schema '
            "WHERE type IN ('table', 'view') AND name = ? COLLATE NOCASE", (table,)
        ).fetchone()
        if found is None:
            raise TableNotFoundError(f'Table not found: {schema}.{table}')
        rows = cursor.execute(
            f'PRAGMA {namespace}.table_info({sqlite_quote_identifier(found[0])})'
        ).fetchall()
        return tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])
    finally:
        cursor.close()


def sqlite_foreign_keys(
    connection: sqlite3.Connection, table: str, schema: str = "main", *,
    target_table: str | None = None,
) -> list[ForeignKey]:
    """Read SQLite keys, optionally filtering targets before resolving implicit keys."""
    cursor = connection.cursor()
    cursor.row_factory = None
    try:
        namespace = sqlite_quote_identifier(schema)
        found = cursor.execute(
            f"SELECT name FROM {namespace}.sqlite_schema "
            "WHERE type IN ('table', 'view') AND name = ? COLLATE NOCASE", (table,)
        ).fetchone()
        if found is None:
            raise TableNotFoundError(f"Table not found: {schema}.{table}")
        table = found[0]
        rows = cursor.execute(
            f"PRAGMA {namespace}.foreign_key_list({sqlite_quote_identifier(table)})"
        ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(row)
        result = []
        for key_id in sorted(grouped):
            parts = sorted(grouped[key_id], key=lambda row: row[1])
            target = parts[0][2]
            if (target_table is not None
                    and sqlite_identifier_key(target) != sqlite_identifier_key(target_table)):
                continue
            target_columns = tuple(row[4] for row in parts)
            if any(column is None for column in target_columns):
                info = cursor.execute(
                    f"PRAGMA {namespace}.table_info({sqlite_quote_identifier(target)})"
                ).fetchall()
                primary_key = sorted((row for row in info if row[5]), key=lambda row: row[5])
                if len(primary_key) != len(parts):
                    raise ValueError(f"Cannot resolve referenced primary key: {schema}.{target}")
                target_columns = tuple(row[1] for row in primary_key)
            result.append(ForeignKey(
                None, schema, table, tuple(row[3] for row in parts),
                schema, target, target_columns,
            ))
        return result
    finally:
        cursor.close()


class SQLiteBackend:
    label = "SQLite"
    dialect = SQLITE

    def base_table(self, query):
        return derived_table(query)

    def __init__(self, connection_string):
        if not connection_string.startswith("sqlite:///"):
            raise ValueError("Use sqlite:///path/to/file for SQLite.")
        path = connection_string[len("sqlite:///"):]
        if not path or path == ":memory:":
            raise ValueError("Choose an existing SQLite database file.")
        self._path = Path(path).resolve()
        self._uri = self._path.as_uri() + "?mode=ro"
        self._connection_string = connection_string
        self._connection = None

    def validate_application_database(self, path):
        target = Path(path).resolve()
        if self._path == target or (self._path.exists() and target.exists() and self._path.samefile(target)):
            raise ValueError('The application database must be separate from the browsed database.')

    def new_session(self):
        return type(self)(self._connection_string)

    @property
    def connection(self):
        # Open on the thread performing the first read, including SQLite setup SQL.
        if self._connection is None:
            connection = sqlite3.connect(self._uri, uri=True)
            try:
                connection.execute("PRAGMA query_only = ON")
            except sqlite3.Error:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def resolve_table(self, table):
        row = self.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') AND name = ? COLLATE NOCASE",
            (table,)).fetchone()
        if row is None:
            raise TableNotFoundError(f"Table not found: {table}")
        return 'main', row[0]

    def relationships(self, table):
        outgoing = sqlite_foreign_keys(self.connection, table)
        result = [Relationship(key.target_table, key.target_columns, key.source_columns,
                               "parent") for key in outgoing]
        names = self.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        ).fetchall()
        for (name,) in names:
            for key in sqlite_foreign_keys(self.connection, name, target_table=table):
                result.append(Relationship(key.source_table, key.source_columns,
                                           key.target_columns, "child"))
        return result

    def query(self, sql, limit, parameters=()):
        validate_limit(limit)
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                   sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def authorize(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() == "load_extension":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        deadline = time.monotonic() + 2
        self.connection.set_authorizer(authorize)
        self.connection.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        try:
            cursor = self.connection.execute(sql, parameters)
            try:
                if cursor.description is None:
                    raise ValueError("Enter a SELECT query that returns rows.")
                columns = tuple(item[0] for item in cursor.description)
                return columns, cursor.fetchmany(limit)
            finally:
                cursor.close()
        finally:
            self.connection.set_authorizer(None)
            self.connection.set_progress_handler(None, 0)

    def table_columns(self, relationship):
        table = sqlite_sql_identifier(relationship.table)
        return self.query(f'SELECT * FROM {table} LIMIT 0', 1)[0]

    def primary_key(self, table, schema=None):
        return sqlite_primary_key(self.connection, table, schema or 'main')

    def row_navigation_query(self, relationship, columns, row, limit, selected_columns=None, order_by=()):
        key = self.primary_key(relationship.table, relationship.schema)
        self.dialect.validate_row_key(key, columns, row)
        target = replace(relationship, columns=key, selected_columns=key)
        return self.navigation_query(target, columns, row, limit, selected_columns, order_by)

    def execute_related_query(self, query, limit):
        """Execute the same bound query whose literal form is shown in the UI."""
        return self.query(query.statement, limit, query.parameters)

    def _related_parts(self, relationship, columns, row, selected_columns, order_by):
        values = relationship_values(relationship, columns, row, self.dialect)
        table, names = sqlite_relationship_identifiers(relationship)
        projection, order = selected_sql(
            selected_columns, order_by, identifier=lambda name: f'{table}.{sqlite_sql_identifier(name)}')
        return table, names, values, projection, order

    def navigation_query(self, relationship, columns, row, limit, selected_columns=None, order_by=()):
        table, names, values, projection, order = self._related_parts(
            relationship, columns, row, selected_columns, order_by)
        return navigation_query(table, names, values, limit, '?', sqlite_literal, projection, order)
