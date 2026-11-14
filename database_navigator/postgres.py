"""PostgreSQL dialect, read-only backend, connections, and metadata discovery."""

from contextlib import contextmanager
from dataclasses import replace
import re

from .models import ForeignKey, Relationship, TableNotFoundError, validate_limit
from .queries import (derived_reference, navigation_query, parse_table, primary_key_values,
                      relationship_values, selected_sql)


# PostgreSQL 18 keywords that cannot be ordinary table/column names (ColId):
# RESERVED_KEYWORD and TYPE_FUNC_NAME_KEYWORD. Keep validation offline and pure.
# https://github.com/postgres/postgres/blob/REL_18_STABLE/src/include/parser/kwlist.h
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

    def split_reference(self, reference):
        return parse_table(reference, postgres=True)

    def relationship_schema(self, schema):
        return schema

    def validate_row_key(self, key, columns, row):
        return primary_key_values(key, columns, row, self.identifier_key)


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
                          *, incoming: bool = False) -> list[ForeignKey]:
    """Read outgoing (or incoming) keys; omitted schema follows search_path."""
    from psycopg.rows import tuple_row

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
        relation_column = "confrelid" if incoming else "conrelid"
        cursor.execute(
            f"""
            SELECT fk.conname, source_ns.nspname, source.relname,
                   ns.nspname, target.relname,
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
            JOIN pg_catalog.pg_class source ON source.oid = fk.conrelid
            JOIN pg_catalog.pg_namespace source_ns ON source_ns.oid = source.relnamespace
            JOIN pg_catalog.pg_class target ON target.oid = fk.confrelid
            JOIN pg_catalog.pg_namespace ns ON ns.oid = target.relnamespace
            WHERE fk.{relation_column} = %s AND fk.contype = 'f'
            ORDER BY fk.conname, fk.oid
            """, (oid,),
        )
        return [ForeignKey(
            name, source_schema, source_table, tuple(source_columns),
            target_schema, target_table, tuple(target_columns),
        ) for name, source_schema, source_table, target_schema, target_table, source_columns, target_columns
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
        schema, table = derived_reference(query, postgres=True)
        return qualified_table(table, schema)

    def resolve_table(self, table):
        # to_regclass follows search_path for unqualified names.
        with read_operation(self.connection):
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.oid = pg_catalog.to_regclass(%s)", (table,))
                row = cursor.fetchone()
        if row is None:
            raise TableNotFoundError(f"Table not found: {table}")
        return tuple(row)

    def relationships(self, table):
        schema, name = parse_table(table, postgres=True)
        table = name
        with read_operation(self.connection):
            outgoing = postgres_foreign_keys(self.connection, table, schema)
            incoming = postgres_foreign_keys(self.connection, table, schema, incoming=True)
        result = [Relationship(key.target_table, key.target_columns, key.source_columns,
                             "parent", key.target_schema)
                for key in outgoing] + [
                    Relationship(key.source_table, key.source_columns, key.target_columns,
                                 "child", key.source_schema)
                    for key in incoming]
        return result

    def query(self, sql, limit, parameters=()):
        validate_limit(limit)
        # A subquery accepts only row-producing SELECT/WITH/VALUES syntax, excludes
        # transaction/session commands and DML, and applies a server-side row cap.
        # Extended protocol (prepare=True) rejects stacked statements even without
        # bind parameters. READ ONLY is additionally enforced by PostgreSQL.
        sql = without_query_terminator(sql)
        bounded = f"SELECT * FROM (\n{sql}\n) AS navigator_result LIMIT {limit}"
        return self._read_bounded(bounded, limit, parameters)

    def _read_bounded(self, sql, limit, parameters=(), *, prepare=True):
        """Execute only internally generated or already-wrapped bounded SQL."""
        validate_limit(limit)
        with read_operation(self.connection):
            with self.connection.cursor() as cursor:
                cursor.execute(sql, parameters or None, prepare=prepare)
                columns = tuple(item.name for item in cursor.description)
                return columns, cursor.fetchmany(limit)

    def table_columns(self, relationship):
        table = qualified_table(relationship.table, relationship.schema)
        return self._read_bounded(f'SELECT * FROM {table} LIMIT 0', 1)[0]

    def primary_key(self, table, schema=None):
        with read_operation(self.connection):
            return postgres_primary_key(self.connection, table, schema)

    def row_navigation_query(self, relationship, columns, row, limit, selected_columns=None, order_by=()):
        key = self.primary_key(relationship.table, relationship.schema)
        self.dialect.validate_row_key(key, columns, row)
        target = replace(relationship, columns=key, selected_columns=key)
        return self.navigation_query(target, columns, row, limit, selected_columns, order_by)

    def execute_related_query(self, query, limit):
        # Generated queries always bind the row limit (and relationship keys),
        # so psycopg uses single-statement extended protocol without PREPARE.
        # Execute directly without a separate statement-preparation round trip.
        return self._read_bounded(query.statement, limit, query.parameters,
                                  prepare=not bool(query.parameters))

    def _related_parts(self, relationship, columns, row, selected_columns, order_by):
        values = relationship_values(relationship, columns, row, self.dialect)
        table = qualified_table(relationship.table, relationship.schema)
        names = tuple(map(postgres_identifier, relationship.columns))
        projection, order = selected_sql(selected_columns, order_by, identifier=postgres_identifier)
        return table, names, values, projection, order

    def navigation_query(self, relationship, columns, row, limit, selected_columns=None, order_by=()):
        import psycopg
        from psycopg import sql

        table, names, values, projection, order = self._related_parts(
            relationship, columns, row, selected_columns, order_by)
        try:
            return navigation_query(table, names, values, limit, '%s',
                                    lambda value: sql.Literal(value).as_string().strip(), projection, order)
        except psycopg.Error as exc:
            raise ValueError(f"Cannot display this relationship key ({type(exc).__name__}).") from None
