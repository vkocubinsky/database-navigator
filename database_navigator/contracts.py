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

"""Contracts between application workflows and read-only database adapters.

Dialect operations are pure. Backend sessions own their connections and must be
created, used and closed on a single worker. Neither contract exposes a driver
connection or its credentials to application code.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from .navigation import Query
from .metadata_models import Table, TableDetails, TableRef, TableSummary


__all__ = ["DatabaseBackend", "DatabaseDialect", "TableNotFoundError"]


class TableNotFoundError(LookupError):
    """The requested table does not exist in the selected schema/search path."""


@runtime_checkable
class DatabaseDialect(Protocol):
    """Pure naming and row-matching rules for one database engine.

    These operations never open a connection or look up database objects.
    Use DatabaseBackend.resolve() to find a table and obtain its resolved TableRef.
    """

    @property
    def supports_schemas(self) -> bool:
        """Whether the application offers schema selection and qualified labels.

        True for PostgreSQL; False for SQLite, which this app limits to main.
        """
        ...

    @property
    def default_schema(self) -> str:
        """Default input value: 'main' for SQLite, '' for PostgreSQL.

        PostgreSQL's empty value leaves the schema unspecified; the backend
        later resolves it using the connection's search_path.
        """
        ...

    def identifier_key(self, name: str) -> str:
        """Return a comparison key for an already-parsed identifier.

        SQLite folds ASCII letters to lowercase; PostgreSQL preserves spelling.
        This does not quote or validate the name.
        """
        ...

    def validate_identifier(self, name: str) -> None:
        """Raise ValueError if a name cannot be used by this app's SQL generator.

        SQLite accepts nonempty names without NUL. PostgreSQL requires names
        usable as unquoted lowercase identifiers. Does not check existence.
        """
        ...

    def validate_schema(self, schema: str) -> None:
        """Validate an explicit schema name without checking its existence.

        SQLite allows only 'main'; PostgreSQL applies its identifier rules.
        Invalid or unsupported names raise ValueError.
        """
        ...

    def table_reference(self, table: str, schema: str | None = None) -> str:
        """Format literal name parts as input for DatabaseBackend.resolve(); no lookup.

        PostgreSQL: ('orders', 'sales') becomes 'sales.orders'; an omitted
        schema produces 'orders'. SQLite: ('orders', 'main') becomes 'orders'.
        Unsupported names/schemas raise ValueError.

        The result is a string, not a TableRef or relationship. It is not a
        general SQL renderer: SQLite preserves literal dots, spaces, and quotes
        in table names without SQL escaping. Do not interpolate it into SQL.
        """
        ...

    def split_reference(self, reference: str) -> tuple[str | None, str]:
        """Split a reference into (schema, table) without resolving it.

        PostgreSQL parses SQL quoting and folds unquoted names to lowercase;
        an omitted schema returns None. SQLite returns ('main', reference),
        treating the entire string, including dots, as a literal table name.
        """
        ...

    def validate_row_key(self, key: tuple[str, ...], columns: tuple[str, ...], row: tuple) -> tuple:
        """Extract row values in primary-key order using identifier comparison.

        Raise ValueError for an empty key, missing or ambiguous result columns,
        or NULL key values. Uses the supplied data only; no metadata lookup.
        """
        ...

    @property
    def placeholder(self) -> str:
        """Value placeholder for bound SQL: '?' or '%s'."""
        ...

    def sql_table(self, ref: TableRef) -> str:
        """Validate and render the destination table for generated SQL."""
        ...

    def sql_column(self, ref: TableRef, name: str) -> str:
        """Validate and render a column, qualifying it when the engine requires it."""
        ...

    def sql_literal(self, value: object) -> str:
        """Render a value for display only; execution always binds original values."""
        ...


@runtime_checkable
class DatabaseBackend(Protocol):
    """One read-only database session, backed by sqlite3 or psycopg.

    The application uses resolve(), table(), and details() for metadata and
    query() for SQL execution.
    execute_related_query() executes bound Query values prepared by navigation.
    Connections open lazily on the first database operation. Each worker owns
    a separate session; database operations and close() run on that worker.
    """

    @property
    def label(self) -> str:
        """Display name of the database engine, such as 'SQLite' or 'PostgreSQL'."""
        ...

    @property
    def dialect(self) -> DatabaseDialect:
        """Pure naming and row-matching rules; accessing them does not connect."""
        ...

    def new_session(self) -> 'DatabaseBackend':
        """Create an independent session for the same database, without connecting.

        Used by the worker pool so workers never share a driver connection.
        """
        ...

    def close(self) -> None:
        """Close this session's connection, if opened, on its owning worker.

        Closing an unopened or already-closed session is harmless.
        """
        ...

    def validate_application_database(self, path: str | Path) -> None:
        """Check that writable application storage is separate from this database.

        SQLite raises ValueError for the same file, including symlink/hardlink
        aliases. PostgreSQL needs no file check. Does not open a connection or
        create the application database; filesystem checks may occur.
        """
        ...

    def base_table(self, query: str) -> str:
        """Extract a table-name string from SQL, without looking up the table.

        Uses a '-- derived: table_name' comment when supplied, otherwise a
        simple first-FROM heuristic. This is not a full SQL parser. Raises
        ValueError if no usable reference can be extracted. Pass the result
        to resolve() to obtain a resolved TableRef.
        """
        ...

    def list_tables(self) -> tuple[TableSummary, ...]:
        """List user tables and views, ordered by schema then stored name.

        Includes PostgreSQL partitioned/foreign tables and materialized views,
        and SQLite virtual tables in the owning connection's schemas. Excludes
        PostgreSQL pg_* / information_schema namespaces and SQLite sqlite_*
        internal objects and virtual-table shadow tables. Names are literal;
        discovery does not require that generated SQL can address each name.
        Runs read-only on the owning worker without loading columns,
        relationships, preferences, or data rows. Filtering belongs to callers.
        """
        ...

    def resolve(self, name: str) -> TableRef:
        """Look up a table reference using this session's connection.

        PostgreSQL accepts SQL table references and follows search_path when
        the schema is omitted. SQLite accepts literal names in the main schema.
        Returns the resolved schema and stored name, without loading columns or
        relationships. Raises TableNotFoundError if absent. Runs on the worker.
        """
        ...

    def table(self, ref: TableRef) -> Table:
        """Read columns, primary key, and kind for a table or view on the worker.

        Does not discover relationships. Raises TableNotFoundError if absent.
        """
        ...

    def details(self, ref: TableRef) -> TableDetails:
        """Read table metadata with relationships to its parents and children.

        Both relationship collections retain child-to-parent direction; a
        self-reference appears in both. Runs on the owning worker and raises
        TableNotFoundError if the table is absent.
        """
        ...

    def query(self, sql: str, limit: int, parameters: tuple = ()) -> tuple[tuple[str, ...], list[tuple]]:
        """Execute a read-only, row-returning statement and fetch at most limit rows.

        Returns (column_names, rows): names are a tuple in result-column order,
        and rows are a list of tuples in that same order. Values retain driver
        types; SQL NULL becomes None.

        Bind values through parameters using the driver's placeholders ('?'
        for SQLite, '%s' for PostgreSQL). Do not interpolate values into SQL.
        Limit must be an integer from 1 to 10000, excluding bool. The adapter
        enforces read-only execution, bounded results, and execution timeouts.
        Invalid input or database failures raise exceptions to the caller.
        """
        ...

    def execute_related_query(self, query: Query, limit: int) -> tuple[tuple[str, ...], list[tuple]]:
        """Execute an internally generated navigation query on the owning worker.

        Uses Query.statement and Query.parameters, never the display text.
        Returns the same (column_names, rows) shape as query(), fetching at most
        limit rows (1..10000). PostgreSQL executes the generated bounded SQL
        directly; user-entered SQL must go through query() and its validation.
        """
        ...
