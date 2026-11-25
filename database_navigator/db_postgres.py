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

"""PostgreSQL dialect, read-only backend, connections, and metadata discovery."""

from contextlib import contextmanager
import re
from typing import Literal

from .contracts import TableNotFoundError
from .metadata_models import Column, Relationship, Table, TableDetails, TableRef, TableSummary
from .query import base_table_for_query, parse_table, primary_key_values, validate_limit


# PostgreSQL 18 keywords that cannot be ordinary table/column names (ColId):
# RESERVED_KEYWORD and TYPE_FUNC_NAME_KEYWORD. Keep validation offline and pure.
# https://github.com/postgres/postgres/blob/REL_18_STABLE/src/include/parser/kwlist.h


__all__ = [
    "PostgreSQLBackend",
    "PostgreSQLDialect",
    "PostgreSQLError",
    "POSTGRESQL",
    "connect_read_only",
    "read_operation",
    "postgres_primary_key",
    "postgres_foreign_keys",
]


_POSTGRES_QUOTED_NAMES = frozenset('''
all analyse analyze and any array as asc asymmetric authorization binary both case cast
check collate collation column concurrently constraint create cross current_catalog
current_date current_role current_schema current_time current_timestamp current_user
default deferrable desc distinct do else end except false fetch for foreign freeze from
full grant group having ilike in initially inner intersect into is isnull join lateral
leading left like limit localtime localtimestamp natural not notnull null offset on only or
order outer overlaps placing primary references returning right select session_user similar
some symmetric system_user table tablesample then to trailing true union unique user using
variadic verbose when where window with
'''.split())


def postgres_identifier(name):
    """Generate bare identifiers only; never silently fold case or splice SQL."""
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z_][a-z0-9_$]*', name)
            or name in _POSTGRES_QUOTED_NAMES):
        raise ValueError(f"Generated PostgreSQL SQL requires an unquoted lowercase identifier: {name!r}.")
    return name


def qualified_table(table, schema=None):
    return (f'{postgres_identifier(schema)}.{postgres_identifier(table)}'
            if schema is not None else postgres_identifier(table))


class PostgreSQLDialect:
    supports_schemas = True
    default_schema = ''  # An omitted schema follows the connection's search_path.

    def identifier_key(self, name):
        return name

    def validate_identifier(self, name):
        postgres_identifier(name)

    def validate_schema(self, schema):
        self.validate_identifier(schema)

    def table_reference(self, table, schema=None):
        return qualified_table(table, schema or None)

    def reference_from_sql(self, token):
        schema, table = self.split_reference(token)
        return self.table_reference(table, schema)

    def split_reference(self, reference):
        return parse_table(reference, postgres=True)

    def validate_row_key(self, key, columns, row):
        return primary_key_values(key, columns, row, self.identifier_key)

    placeholder = '%s'

    def sql_table(self, ref):
        return qualified_table(ref.name, ref.schema)

    def sql_column(self, ref, name):
        return postgres_identifier(name)

    def sql_literal(self, value):
        import psycopg
        from psycopg import sql

        try:
            return sql.Literal(value).as_string().strip()
        except psycopg.Error as exc:
            raise ValueError(f"Cannot display this relationship key ({type(exc).__name__}).") from None


POSTGRESQL = PostgreSQLDialect()


def without_query_terminator(sql):
    """Remove one final SQL semicolon, preserving trailing comments and literals.

    This is a lexical scan, not statement validation. Stacked statements retain
    their internal separators and remain subject to the prepared-query wrapper.
    Leave unterminated quotes/comments untouched for PostgreSQL to diagnose.
    """
    sql = sql.rstrip()
    index = 0
    terminator = None
    while index < len(sql):
        if sql[index].isspace():
            index += 1
            continue
        if sql.startswith('--', index):
            end = re.search(r'[\r\n]', sql[index + 2:])
            index = len(sql) if end is None else index + 2 + end.end()
            continue
        if sql.startswith('/*', index):
            depth = 1
            index += 2
            while index < len(sql) and depth:
                if sql.startswith('/*', index):
                    depth += 1
                    index += 2
                elif sql.startswith('*/', index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                return sql
            continue
        terminator = index if sql[index] == ';' else None
        escaped = sql[index] in 'eE' and sql[index:index + 2].lower() == "e'"
        if sql[index] in "\"'" or escaped:
            if escaped:
                index += 1
            quote_char = sql[index]
            index += 1
            while index < len(sql):
                if escaped and sql[index] == '\\':
                    index += 2
                elif sql[index] == quote_char:
                    index += 1
                    if index < len(sql) and sql[index] == quote_char:
                        index += 1
                    else:
                        break
                else:
                    index += 1
            else:
                return sql
            continue
        dollar = re.match(r'\$(?:[^\W\d]\w*)?\$', sql[index:]) if sql[index] == '$' else None
        if dollar:
            delimiter = dollar[0]
            end = sql.find(delimiter, index + len(delimiter))
            if end < 0:
                return sql
            index = end + len(delimiter)
            continue
        # Consume identifiers as a whole: embedded $ is not a dollar quote,
        # and an identifier ending in E is not an escape-string prefix.
        identifier = re.match(r'[^\W\d][\w$]*', sql[index:])
        index += len(identifier[0]) if identifier else 1
    if terminator is not None:
        return sql[:terminator] + sql[terminator + 1:]
    return sql


class PostgreSQLError(RuntimeError):
    """A PostgreSQL error safe to display without connection credentials."""


def connect_read_only(connection_string):
    try:
        import psycopg
    except ImportError:
        raise ValueError("PostgreSQL requires psycopg; run ./setup-env.sh") from None
    connection = None
    try:
        connection = psycopg.connect(
            connection_string, autocommit=True, connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000 "
                    "-c lock_timeout=2000",
        )
        connection.read_only = True
        return connection
    except psycopg.Error as exc:
        if connection is not None:
            connection.close()
        raise PostgreSQLError(
            f"PostgreSQL connection failed ({type(exc).__name__}); "
            "check VPN/proxy, libpq credentials, and access."
        ) from None


@contextmanager
def read_operation(connection):
    """Translate driver errors; autocommit ends each read's implicit transaction."""
    import psycopg

    try:
        # default_transaction_read_only is set at connection startup. Send only
        # the query; PostgreSQL ends its transaction on success or failure.
        yield connection
    except psycopg.Error as exc:
        raise PostgreSQLError(f"PostgreSQL query failed ({type(exc).__name__}).") from None


def postgres_primary_key(connection, table: str, schema: str | None = None) -> tuple[str, ...]:
    """Return declared primary-key columns; omitted schema follows search_path."""
    from psycopg.rows import tuple_row

    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            """
            SELECT ARRAY(
                SELECT a.attname
                FROM pg_catalog.pg_constraint pk
                CROSS JOIN LATERAL unnest(pk.conkey) WITH ORDINALITY AS k(num, position)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = pk.conrelid AND a.attnum = k.num
                WHERE pk.conrelid = c.oid AND pk.contype = 'p'
                ORDER BY k.position)
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
              AND ((%s::text IS NULL AND pg_catalog.pg_table_is_visible(c.oid))
                   OR n.nspname = %s)
            """, (table, schema, schema),
        )
        found = cursor.fetchone()
        if found is None:
            raise TableNotFoundError(f"Table not found: {schema + '.' if schema else ''}{table}")
        return tuple(found[0])


def postgres_foreign_keys(connection, table: str, schema: str | None = None,
                          *, direction: Literal['parent', 'child'] = 'parent') -> list[Relationship]:
    """Read relationships leading to the selected table’s parents or children."""
    from psycopg.rows import tuple_row

    if direction not in ('parent', 'child'):
        raise ValueError('Unknown navigation direction.')
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            """
            SELECT c.oid, n.nspname, c.relname
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
              AND ((%s::text IS NULL AND pg_catalog.pg_table_is_visible(c.oid))
                   OR n.nspname = %s)
            """, (table, schema, schema),
        )
        found = cursor.fetchone()
        if found is None:
            raise TableNotFoundError(f"Table not found: {schema + '.' if schema else ''}{table}")
        oid = found[0]
        # This fragment is selected internally, never supplied by the caller.
        relation_column = "confrelid" if direction == "child" else "conrelid"
        cursor.execute(
            f"""
            SELECT fk.conname, child_ns.nspname, child.relname,
                   parent_ns.nspname, parent.relname,
                   ARRAY(SELECT a.attname
                         FROM unnest(fk.conkey) WITH ORDINALITY AS k(num, position)
                         JOIN pg_catalog.pg_attribute a
                           ON a.attrelid = fk.conrelid AND a.attnum = k.num
                         ORDER BY k.position),
                   ARRAY(SELECT a.attname
                         FROM unnest(fk.confkey) WITH ORDINALITY AS k(num, position)
                         JOIN pg_catalog.pg_attribute a
                           ON a.attrelid = fk.confrelid AND a.attnum = k.num
                         ORDER BY k.position)
            FROM pg_catalog.pg_constraint fk
            JOIN pg_catalog.pg_class child ON child.oid = fk.conrelid
            JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child.relnamespace
            JOIN pg_catalog.pg_class parent ON parent.oid = fk.confrelid
            JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            WHERE fk.{relation_column} = %s AND fk.contype = 'f'
            ORDER BY fk.conname, fk.oid
            """, (oid,),
        )
        return [Relationship(
            parent_table=TableRef(parent_schema, parent_table),
            child_table=TableRef(child_schema, child_table),
            parent_key=tuple(parent_key),
            child_key=tuple(child_key),
            origin='database',
            name=name,
        ) for name, child_schema, child_table, parent_schema, parent_table, child_key, parent_key
            in cursor.fetchall()]


class PostgreSQLBackend:
    label = "PostgreSQL"
    dialect = POSTGRESQL

    def __init__(self, connection_string):
        self._connection_string = connection_string
        self._connection = None

    def validate_application_database(self, path):
        # A remote PostgreSQL database cannot alias the local preference file.
        pass

    def new_session(self):
        return type(self)(self._connection_string)

    @property
    def connection(self):
        # Local driver state only: no health-check query on the normal read path.
        # A failed read is reported once; the next request replaces a broken connection.
        if self._connection is None or self._connection.closed:
            self._connection = connect_read_only(self._connection_string)
        return self._connection

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def base_table(self, query):
        return base_table_for_query(query, self.dialect)

    def list_tables(self) -> tuple[TableSummary, ...]:
        from psycopg.rows import tuple_row

        connection = self.connection
        with read_operation(connection), connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute('''
                SELECT n.nspname, c.relname, c.relkind
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm')
                  AND n.nspname NOT LIKE 'pg!_%' ESCAPE '!'
                  AND n.nspname <> 'information_schema'
                ORDER BY n.nspname, c.relname
            ''')
            rows = cursor.fetchall()
        return tuple(TableSummary(TableRef(schema, name),
                                  {'v': 'view', 'm': 'materialized_view'}.get(kind, 'table'))
                     for schema, name, kind in rows)

    def resolve(self, name: str) -> TableRef:
        # to_regclass follows search_path for unqualified names.
        connection = self.connection
        with read_operation(connection):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.oid = pg_catalog.to_regclass(%s) "
                    "AND c.relkind IN ('r', 'p', 'f', 'v', 'm')", (name,))
                row = cursor.fetchone()
        if row is None:
            raise TableNotFoundError(f"Table not found: {name}")
        return TableRef(*row)

    def table(self, ref):
        from psycopg.rows import tuple_row

        connection = self.connection
        with read_operation(connection), connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute('''
                SELECT c.oid, c.relkind
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                  AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
            ''', (ref.schema, ref.name))
            found = cursor.fetchone()
            if found is None:
                raise TableNotFoundError(f'Table not found: {ref.schema}.{ref.name}')
            oid, kind = found
            cursor.execute('''
                SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
                       NOT a.attnotnull
                FROM pg_catalog.pg_attribute a
                WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY a.attnum
            ''', (oid,))
            columns = tuple(Column(*row) for row in cursor.fetchall())
            primary = postgres_primary_key(connection, ref.name, ref.schema)
        return Table(ref, columns, primary, {'v': 'view', 'm': 'materialized_view'}.get(kind, 'table'))

    def details(self, ref: TableRef) -> TableDetails:
        table = self.table(ref)
        return TableDetails(table, parent_relationships=self._parent_relationships(table.ref),
                            child_relationships=self._child_relationships(table.ref))

    def _parent_relationships(self, ref):
        connection = self.connection
        with read_operation(connection):
            return tuple(postgres_foreign_keys(connection, ref.name, ref.schema))

    def _child_relationships(self, ref):
        connection = self.connection
        with read_operation(connection):
            return tuple(postgres_foreign_keys(connection, ref.name, ref.schema, direction='child'))

    def query(self, sql, limit, parameters=()):
        validate_limit(limit)
        # A subquery accepts only row-producing SELECT/WITH/VALUES syntax, excludes
        # transaction/session commands and DML, and applies a server-side row cap.
        # Extended protocol (prepare=True) rejects stacked statements even without
        # bind parameters. READ ONLY is additionally enforced by PostgreSQL.
        sql = without_query_terminator(sql)
        bounded = f"SELECT * FROM (\n{sql}\n) AS navigator_result LIMIT {limit}"
        with read_operation(self.connection):
            with self.connection.cursor() as cursor:
                cursor.execute(bounded, parameters or None, prepare=True)
                columns = tuple(item.name for item in cursor.description)
                return columns, cursor.fetchmany(limit)
