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

from itertools import groupby
from operator import itemgetter
from pathlib import Path
import math
import re
import sqlite3
import time

from .contracts import TableNotFoundError
from .metadata_models import Column, Relationship, Table, TableDetails, TableRef, TableSummary
from .navigation import (derived_reference, primary_key_values, validate_limit)


__all__ = [
    "SQLiteBackend",
    "SQLiteDialect",
    "SQLITE",
    "sqlite_primary_key",
    "sqlite_foreign_keys",
]


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

    def validate_row_key(self, key, columns, row):
        return primary_key_values(key, columns, row, self.identifier_key)

    placeholder = '?'

    def sql_table(self, ref):
        self.validate_schema(ref.schema)
        self.validate_identifier(ref.name)
        return sqlite_sql_identifier(ref.name)

    def sql_column(self, ref, name):
        self.validate_identifier(name)
        return f'{self.sql_table(ref)}.{sqlite_sql_identifier(name)}'

    def sql_literal(self, value):
        return sqlite_literal(value)


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


def _sqlite_relationships(rows, schema, primary_key_for):
    """Rows are ordered by child table, constraint id, and column position."""
    for (child, key_id, parent), parts in groupby(rows, key=itemgetter(0, 1, 2)):
        child_key, parent_key = zip(*(row[3:] for row in parts))
        if None in parent_key:
            parent_key = primary_key_for(parent)
            if len(parent_key) != len(child_key):
                raise ValueError(f"Cannot resolve referenced primary key: {schema}.{parent}")
        yield Relationship(parent_table=TableRef(schema, parent), child_table=TableRef(schema, child),
                           parent_key=parent_key, child_key=child_key, origin='database',
                           relationship_id=key_id)


def sqlite_foreign_keys(
    connection: sqlite3.Connection, table: str, schema: str = "main", *,
    parent_table: str | None = None,
) -> list[Relationship]:
    """Read SQLite keys, optionally filtering parent tables before resolving implicit keys."""
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

        def primary_key_for(parent):
            info = cursor.execute(
                f"PRAGMA {namespace}.table_info({sqlite_quote_identifier(parent)})"
            ).fetchall()
            return tuple(row[1] for row in sorted(info, key=itemgetter(5)) if row[5])

        target = sqlite_identifier_key(parent_table) if parent_table is not None else None
        ordered = ((table, row[0], *row[2:5]) for row in sorted(rows, key=itemgetter(0, 1))
                   if target is None or sqlite_identifier_key(row[2]) == target)
        return list(_sqlite_relationships(ordered, schema, primary_key_for))
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

    def list_tables(self) -> tuple[TableSummary, ...]:
        rows = self.connection.execute('''
            SELECT schema, name, CASE type WHEN 'virtual' THEN 'table' ELSE type END
            FROM pragma_table_list
            WHERE type IN ('table', 'view', 'virtual')
              AND substr(name, 1, 7) <> 'sqlite_'
            ORDER BY schema, name
        ''').fetchall()
        return tuple(TableSummary(TableRef(schema, name), kind) for schema, name, kind in rows)

    def resolve(self, name: str) -> TableRef:
        row = self.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') AND name = ? COLLATE NOCASE",
            (name,)).fetchone()
        if row is None:
            raise TableNotFoundError(f"Table not found: {name}")
        return TableRef('main', row[0])

    def table(self, ref):
        connection = self.connection
        namespace = sqlite_quote_identifier(ref.schema)
        cursor = connection.cursor()
        cursor.row_factory = None
        try:
            found = cursor.execute(
                f'SELECT name, type FROM {namespace}.sqlite_schema '
                "WHERE type IN ('table', 'view') AND name = ? COLLATE NOCASE", (ref.name,)
            ).fetchone()
            if found is None:
                raise TableNotFoundError(f'Table not found: {ref.schema}.{ref.name}')
            name, kind = found
            rows = cursor.execute(
                f'PRAGMA {namespace}.table_xinfo({sqlite_quote_identifier(name)})'
            ).fetchall()
            primary = tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])
            # table_xinfo omits the implicit NOT NULL of an INTEGER rowid alias.
            # INTEGER PRIMARY KEY DESC has a separate index and is not an alias.
            integer_key = len(primary) == 1 and any(
                row[5] and row[2].upper() == 'INTEGER' for row in rows)
            rowid_alias = integer_key and not any(row[3] == 'pk' for row in cursor.execute(
                f'PRAGMA {namespace}.index_list({sqlite_quote_identifier(name)})').fetchall())
            columns = tuple(Column(row[1], row[2], not (bool(row[3]) or (rowid_alias and bool(row[5]))))
                            for row in rows)
            return Table(TableRef(ref.schema, name), columns, primary, kind)
        finally:
            cursor.close()

    def details(self, ref: TableRef) -> TableDetails:
        table = self.table(ref)
        return TableDetails(table, parent_relationships=self._parent_relationships(table.ref),
                            child_relationships=self._child_relationships(table))

    def _parent_relationships(self, ref):
        return tuple(sqlite_foreign_keys(self.connection, ref.name, ref.schema))

    def _child_relationships(self, table):
        ref = table.ref
        namespace = sqlite_quote_identifier(ref.schema)
        # Qualify the pragma so a browsed table of the same name cannot shadow it.
        rows = self.connection.execute(f'''
            SELECT t.name, fk.id, fk."table", fk."from", fk."to"
            FROM {namespace}.sqlite_schema AS t
            CROSS JOIN temp.pragma_foreign_key_list(t.name, ?) AS fk
            WHERE t.type = 'table' AND fk."table" = ? COLLATE NOCASE
            ORDER BY t.name, fk.id, fk.seq
        ''', (ref.schema, ref.name))
        try:
            return tuple(_sqlite_relationships(rows, ref.schema, lambda parent: table.primary_key))
        finally:
            rows.close()

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
