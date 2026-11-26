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

"""Navigation queries and backend workflows, independent of UI browsing state."""

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from concurrent.futures import Future
from collections.abc import Mapping, Sequence
from typing import Literal

from .connection import select_backend
from .metadata_models import Relationship, Table, TableDetails, TableRef, TableSummary
from .metadata_cache import MetadataCache
from .query import (Query, base_table_for_query, column_values, derived_reference,
                    parse_table, primary_key_values, validate_limit)
from .storage import ApplicationStore, DEFAULT_DATABASE, validate_relationship, validate_application_database
from .storage_models import NavigationDefault, StoredRelationship
from .worker import DatabasePool
from .contracts import DatabaseBackend, DatabaseDialect


__all__ = [
    "Navigation",
    "NavigationSession",
    "NavigationLink",
    "Query",
    "QueryGenerator",
    "DEFAULT_DATABASE",
    "open_navigation",
    "create_backend",
    "filter_tables",
    "navigation_links",
    "default_link_index",
    "merge_relationships",
    "validate_limit",
    "parse_table",
    "derived_reference",
    "base_table_for_query",
    "primary_key_values",
]


@dataclass(frozen=True)
class NavigationLink:
    """A structural relationship viewed from one endpoint for browsing."""

    relationship: Relationship
    direction: Literal['parent', 'child']
    show_schema: bool = True

    def __post_init__(self):
        if self.direction not in ('parent', 'child'):
            raise ValueError('Unknown navigation direction.')

    @property
    def ref(self):
        return self.relationship.parent_table if self.direction == 'parent' else self.relationship.child_table

    @property
    def table(self):
        return self.ref.name

    @property
    def schema(self):
        return self.ref.schema

    @property
    def columns(self):
        key = self.relationship
        return key.parent_key if self.direction == 'parent' else key.child_key

    @property
    def selected_columns(self):
        key = self.relationship
        return key.child_key if self.direction == 'parent' else key.parent_key

    @property
    def selection(self) -> NavigationDefault:
        key = self.relationship
        child = key.child_table if key.origin == 'database' and self.direction == 'child' else None
        return NavigationDefault(self.direction, key.origin, key.relationship_id, key.name, child)

    @property
    def display_table(self):
        # Labels are not SQL; display even names rejected by query generation.
        return f'{self.schema}.{self.table}' if self.show_schema else self.table

    def notation(self, selected_table):
        """Keep the linked table first; point from the child key to the parent key."""
        arrow = '←' if self.direction == 'parent' else '→'
        return (f"{self.display_table}({', '.join(self.columns)}) {arrow} "
                f"{selected_table}({', '.join(self.selected_columns)})")


def navigation_links(details, *, show_schema=True):
    """Project table relationships into parent/child navigation choices."""
    links = [NavigationLink(key, 'parent', show_schema) for key in details.parent_relationships]
    links += [NavigationLink(key, 'child', show_schema) for key in details.child_relationships]
    # Database constraints take precedence over additional local declarations.
    return sorted(links, key=lambda link: link.relationship.origin != 'database')


def default_link_index(links, value):
    """Restore a saved identifier against the current table's discovered links."""
    if not isinstance(value, NavigationDefault):
        return None
    return next((index for index, link in enumerate(links) if link.selection == value), None)


def filter_tables(tables, search):
    """Filter catalog summaries by name or schema.name without database I/O."""
    pattern = search.casefold()
    wildcard = any(char in pattern for char in '*?[')
    def matches(table):
        ref = table.ref
        name = f'{ref.schema}.{ref.name}' if '.' in pattern else ref.name
        name = name.casefold()
        return fnmatchcase(name, pattern) if wildcard else pattern in name
    return tuple(table for table in tables if matches(table))


def related_query(table, columns, values, limit, placeholder, projection='*', order=''):
    """Build the bound statement for a prepared navigation query."""
    validate_limit(limit)
    predicates = ' AND '.join(f'{name} = {placeholder}' for name in columns)
    where = f' WHERE {predicates}' if columns else ''
    return (f'SELECT {projection} FROM {table}{where}{order} LIMIT {placeholder}',
            (*values, limit))


def navigation_query(table, columns, values, limit, placeholder, literal, projection='*', order='', *,
                     table_ref=None):
    validate_limit(limit)
    display = ' and '.join(f'{name} = {literal(value)}' for name, value in zip(columns, values))
    preview = f'select {projection}\nfrom {table}'
    if columns:
        preview += f'\nwhere {display}'
    if order:
        preview += '\n' + order.lstrip()
    preview += f'\nlimit {limit};'
    statement, parameters = related_query(table, columns, values, limit, placeholder, projection, order)
    return Query(preview, statement, parameters, table_ref)


class QueryGenerator:
    """Generate bound queries from typed row values, column names, and sort terms; no I/O."""

    def __init__(self, dialect: DatabaseDialect):
        self.dialect = dialect

    def _ref(self, table):
        return table.ref if isinstance(table, Table) else table

    def _same_table(self, left, right):
        identity = self.dialect.identifier_key
        return (identity(left.schema), identity(left.name)) == (identity(right.schema), identity(right.name))

    def _options(self, ref, columns, order):
        if isinstance(columns, str) or isinstance(order, str):
            raise ValueError('Supply a sequence of column names and a sequence of sort terms.')

        def render(name, seen):
            sql = self.dialect.sql_column(ref, name)
            key = self.dialect.identifier_key(name)
            if key in seen:
                raise ValueError('Choose each column only once.')
            seen.add(key)
            return sql

        projected = set()
        sorted_names = set()
        projection = ', '.join(render(name, projected) for name in columns) or '*'
        terms = []
        for name, direction in order:
            if direction not in ('ASC', 'DESC'):
                raise ValueError('Sort direction must be ASC or DESC.')
            terms.append(f'{render(name, sorted_names)} {direction}')
        return projection, ' order by ' + ', '.join(terms) if terms else ''

    def query_for_relationship(
        self, table: Table | TableRef, row: Mapping[str, object], link: NavigationLink, *,
        columns: Sequence[str], order: Sequence[tuple[str, str]], limit: int,
    ) -> Query:
        source = link.relationship.child_table if link.direction == 'parent' else link.relationship.parent_table
        if not self._same_table(self._ref(table), source):
            raise ValueError('Selected table does not match the relationship traversal.')
        values = column_values(link.selected_columns, tuple(row), tuple(row.values()),
                               self.dialect.identifier_key)
        return self._query(link.ref, link.columns, values, columns, order, limit)

    def query_for_table(
        self, table: Table | TableRef, *,
        columns: Sequence[str], order: Sequence[tuple[str, str]], limit: int,
    ) -> Query:
        return self._query(self._ref(table), (), (), columns, order, limit)

    def query_for_primary_key(
        self, table: Table | TableRef, row: Mapping[str, object], primary_key: tuple[str, ...], *,
        columns: Sequence[str], order: Sequence[tuple[str, str]], limit: int,
    ) -> Query:
        ref = self._ref(table)
        values = primary_key_values(primary_key, tuple(row), tuple(row.values()),
                                    self.dialect.identifier_key)
        return self._query(ref, primary_key, values, columns, order, limit)

    def _query(self, ref, names, values, columns, order, limit):
        projection, ordering = self._options(ref, columns, order)
        return navigation_query(
            self.dialect.sql_table(ref),
            tuple(self.dialect.sql_column(ref, name) for name in names), values,
            limit, self.dialect.placeholder, self.dialect.sql_literal, projection, ordering, table_ref=ref)


def _table_key(dialect, schema, table):
    return dialect.identifier_key(schema or dialect.default_schema), dialect.identifier_key(table)


class Navigation:
    """Asynchronous navigation workflows using worker-owned backend sessions."""

    def __init__(self, database_backend: DatabaseBackend, *, model=None, application_database=DEFAULT_DATABASE):
        if model is not None:
            validate_configuration(database_backend, model, application_database)
        self._database_backend = database_backend
        self.model = model
        self.label = database_backend.label
        self.supports_schemas = database_backend.dialect.supports_schemas
        self._metadata = MetadataCache()
        session_factory = lambda: NavigationSession(
            database_backend.new_session(), model, application_database, metadata=self._metadata)
        self._pool = DatabasePool(session_factory)

    def close(self):
        """Cancel queued work without waiting; workers close their own connections."""
        self._pool.close()

    def reset_metadata(self):
        self._metadata.reset()

    def base_table(self, statement):
        """Use the generated destination or infer the base table from editor SQL."""
        return base_table_for_query(statement, self._database_backend.dialect)

    def initial_child_table(self, query=None):
        if query is None:
            return self._database_backend.dialect.default_schema, ''
        table = self.base_table(query)
        return self._database_backend.dialect.split_reference(table)

    def table_key(self, schema, table):
        """Pure identity comparison for diagram nodes, including SQLite casing."""
        dialect = self._database_backend.dialect
        return _table_key(dialect, schema, table)

    def validate_row_key(self, key, row):
        return self._database_backend.dialect.validate_row_key(key, tuple(row), tuple(row.values()))

    def query(self, sql, limit, parameters=()) -> Future:
        # Preserve the minimal call signature for backend implementations.
        args = (sql, limit, parameters) if parameters else (sql, limit)
        return self._pool.submit('query', *args)

    def _read_metadata(self, operation, *args):
        # A request queued before reset may finish, but cannot refill the new cache.
        return self._pool.submit(operation, *args, self._metadata.generation)

    def table(self, ref) -> Future:
        return self._read_metadata(self._metadata.table, ref)

    def list_tables(self) -> Future:
        """Return cached catalog summaries, discovered lazily on a worker."""
        return self._read_metadata(self._metadata.list_tables)

    def table_query(self, ref: TableRef, limit: int) -> Future:
        return self._pool.submit('table_query', ref, limit)

    def details(self, ref) -> Future:
        return self._read_metadata(self._metadata.details, ref)

    def navigation_links(self, table) -> Future:
        """Return a future containing parent/child NavigationLink choices."""
        return self._read_metadata('navigation_links', table)

    def relationship_diagram(self, table) -> Future:
        """Return master identity, direct links, and key/relationship columns."""
        return self._read_metadata('relationship_diagram', table)

    def primary_key(self, table, schema=None) -> Future:
        return self._read_metadata('primary_key', table, schema)

    def navigation_query(self, link, row, limit) -> Future:
        return self._pool.submit('navigation_query', link, row, limit)

    def row_navigation_query(self, link, row, limit) -> Future:
        return self._pool.submit('row_navigation_query', link, row, limit)

    def execute_related_query(self, query, limit) -> Future:
        return self._pool.submit('execute_related_query', query, limit)

    def startup_sql(self) -> Future:
        return self._pool.submit('startup_sql')

    def save_startup_sql(self, text) -> Future:
        return self._pool.submit('save_startup_sql', text)

    def navigation_defaults(self, table) -> Future:
        return self._pool.submit('navigation_defaults', table)

    def save_navigation_default(self, table, direction, link=None) -> Future:
        return self._pool.submit('save_navigation_default', table, direction, link)

    def table_preferences(self, link) -> Future:
        return self._pool.submit('table_preferences', link)

    def save_preference(self, link, name, choices) -> Future:
        return self._pool.submit('save_preference', link, name, choices)

    def custom_relationships(self) -> Future:
        return self._pool.submit('custom_relationships')

    def relationship_columns(self, schema, table) -> Future:
        return self._pool.submit('relationship_columns', schema, table)

    def relationship_endpoints(self, child, parent) -> Future:
        """Load fresh column choices for both sides of a relationship on one worker."""
        return self._pool.submit('relationship_endpoints', child, parent)

    def save_custom_relationship(self, identifier, key: Relationship) -> Future:
        return self._pool.submit('save_custom_relationship', identifier, key)

    def delete_custom_relationship(self, identifier) -> Future:
        return self._pool.submit('delete_custom_relationship', identifier)


def create_backend(connection_string, *, backend=None) -> DatabaseBackend:
    """Select a read-only database backend without opening a connection."""
    from .db_sqlite import SQLiteBackend
    from .db_postgres import PostgreSQLBackend

    name = select_backend(connection_string, backend)
    backend_type = {"sqlite": SQLiteBackend, "postgresql": PostgreSQLBackend}[name]
    return backend_type(connection_string)


def open_navigation(connection, *, backend=None, model=None, application_database=DEFAULT_DATABASE):
    """Validate configuration before starting any workers or opening the window."""
    return Navigation(create_backend(connection, backend=backend), model=model,
                      application_database=application_database)


def merge_relationships(details, keys, *, dialect: DatabaseDialect):
    """Merge local declarations without duplicating database constraints."""
    identity = dialect.identifier_key

    def ref_key(ref):
        return identity(ref.schema), identity(ref.name)

    def signature(key):
        return (ref_key(key.child_table), ref_key(key.parent_table),
                tuple(map(identity, key.child_key)), tuple(map(identity, key.parent_key)))

    parent_relationships, child_relationships = list(details.parent_relationships), list(details.child_relationships)
    seen = {signature(key) for key in (*parent_relationships, *child_relationships)}
    selected = ref_key(details.table.ref)
    for key in keys:
        sig = signature(key)
        if sig in seen:
            continue
        if ref_key(key.child_table) == selected:
            parent_relationships.append(key)
        if ref_key(key.parent_table) == selected:
            child_relationships.append(key)
        seen.add(sig)
    return TableDetails(details.table, tuple(parent_relationships), tuple(child_relationships))


def validate_configuration(database_backend: DatabaseBackend, model, path):
    if model is None:
        return
    if not isinstance(model, str) or not model.strip() or '\0' in model:
        raise ValueError('Model must be a nonempty name without NUL.')
    path = validate_application_database(path)
    database_backend.validate_application_database(path)


class NavigationSession:
    """One worker's database backend and lazily opened application storage connection."""

    def __init__(self, database_backend: DatabaseBackend, model, path, *, metadata=None):
        validate_configuration(database_backend, model, path)
        self.database_backend = database_backend
        self.dialect = database_backend.dialect
        self.model = model
        self.path = path
        self._store = None
        self.metadata = metadata if metadata is not None else MetadataCache()

    def resolve(self, name: str) -> TableRef:
        return self.database_backend.resolve(name)

    def list_tables(self) -> tuple[TableSummary, ...]:
        return self.database_backend.list_tables()

    def table(self, ref: TableRef) -> Table:
        return self.database_backend.table(ref)

    def details(self, ref: TableRef) -> TableDetails:
        details = self.database_backend.details(ref)
        if self.model is None:
            return details
        declarations = (replace(record.relationship, relationship_id=record.id)
                        for record in self.store.relationships(self.model, dialect=self.dialect))
        return merge_relationships(details, declarations, dialect=self.dialect)

    def navigation_links(self, table, generation):
        ref = self.metadata.resolve_table(self, table, generation)
        details = self.metadata.details(self, ref, generation)
        return navigation_links(details, show_schema=self.dialect.supports_schemas)

    def primary_key(self, table, schema, generation):
        ref = TableRef(schema, table) if schema else self.metadata.resolve_table(self, table, generation)
        return self.metadata.table(self, ref, generation).primary_key

    def relationship_diagram(self, table, generation):
        """Build the one-hop diagram, reading only table metadata for its endpoints."""
        ref = self.metadata.resolve_table(self, table, generation)
        details = self.metadata.details(self, ref, generation)
        links = navigation_links(details, show_schema=self.dialect.supports_schemas)
        master = _table_key(self.dialect, ref.schema, ref.name)
        endpoints = {master: ref}
        participants = {master: []}
        for link in links:
            key = _table_key(self.dialect, link.schema, link.table)
            endpoints.setdefault(key, link.ref)
            participants.setdefault(key, []).extend(link.columns)
            participants[master].extend(link.selected_columns)

        columns = {}
        for key, endpoint in endpoints.items():
            snapshot = details.table if key == master else self.metadata.table(self, endpoint, generation)
            seen, selected = set(), []
            for names, is_primary in ((snapshot.primary_key, True), (participants[key], False)):
                for name in names:
                    identity = self.dialect.identifier_key(name)
                    if identity not in seen:
                        selected.append((name, is_primary))
                        seen.add(identity)
            columns[key] = tuple(selected)
        return (ref.schema, ref.name), links, columns

    def query(self, sql, limit, parameters=()):
        args = (sql, limit, parameters) if parameters else (sql, limit)
        return self.database_backend.query(*args)

    def execute_related_query(self, query, limit):
        return self.query(query.statement, limit, query.parameters)

    @property
    def store(self):
        if self.model is None:
            raise ValueError('Storage operations require a model.')
        if self._store is None:
            self._store = ApplicationStore(self.path)
        return self._store

    def close(self):
        try:
            self.database_backend.close()
        finally:
            if self._store is not None:
                self._store.close()

    def _destination(self, link, generation):
        table = self.dialect.table_reference(link.table, link.schema)
        return self.metadata.resolve_table(self.database_backend, table, generation)

    def _columns(self, ref, generation):
        table = self.metadata.table(self.database_backend, ref, generation)
        return tuple(column.name for column in table.columns)

    def startup_sql(self):
        """Read local preferences only; never open the browsed database."""
        return self.store.get_model(self.model, 'start_sql') or ''

    def custom_relationships(self):
        return self.store.relationships(
            self.model, dialect=self.dialect)

    def relationship_columns(self, schema, table):
        """Fresh, read-only endpoint discovery, independent of cached navigation metadata."""
        if not isinstance(table, str) or not table or '\0' in table:
            raise ValueError('Enter a table name without NUL.')
        name = self.dialect.table_reference(table, schema)
        ref = self.database_backend.resolve(name)
        self.dialect.validate_schema(ref.schema)
        self.dialect.validate_identifier(ref.name)
        table = self.table(ref)
        return ref.schema, ref.name, tuple(column.name for column in table.columns)

    def relationship_endpoints(self, child, parent):
        return self.relationship_columns(*child), self.relationship_columns(*parent)

    def save_custom_relationship(self, identifier, key: Relationship):
        validate_relationship(key, dialect=self.dialect)
        endpoints = []
        for schema, table, columns in ((key.child_table.schema, key.child_table.name, key.child_key),
                                       (key.parent_table.schema, key.parent_table.name, key.parent_key)):
            schema, table, available = self.relationship_columns(schema, table)
            normalized = {self.dialect.identifier_key(name): name for name in available}
            chosen = []
            for column in columns:
                name = normalized.get(self.dialect.identifier_key(column))
                if name is None:
                    raise ValueError(f'Column {column!r} no longer exists in {schema}.{table}. Reload columns.')
                chosen.append(name)
            endpoints.append((schema, table, tuple(chosen)))
        child, parent = endpoints
        key = replace(key, child_table=TableRef(*child[:2]), parent_table=TableRef(*parent[:2]),
                      child_key=child[2], parent_key=parent[2])
        return self.store.save_relationship(self.model, StoredRelationship(identifier, key), dialect=self.dialect)

    def delete_custom_relationship(self, identifier):
        self.store.delete_relationship(self.model, identifier)

    def save_startup_sql(self, text):
        if text.strip():
            self.store.set_model(self.model, 'start_sql', text)
        else:
            self.store.delete_model(self.model, 'start_sql')

    def table_preferences(self, link):
        # Keep stale or invalid choices editable without validating against metadata.
        generation = self.metadata.generation
        ref = self._destination(link, generation)
        saved = {}
        try:
            selected = self.store.selected_columns(self.model, ref)
            if selected:
                saved['columns'] = selected
        except ValueError as exc:
            saved['columns'] = ()
            saved['columns_error'] = str(exc)
        try:
            order = self.store.sort_order(self.model, ref)
            if order:
                saved['order_by'] = order
        except ValueError as exc:
            saved['order_by'] = ()
            saved['order_by_error'] = str(exc)
        return self._columns(ref, generation), saved

    def navigation_defaults(self, table):
        ref = self.database_backend.resolve(table)
        key = (self.model, ref.schema, ref.name)
        saved = self.store.get_all(*key)
        records = self.store.default_relationships(self.model, ref)
        for record in records:
            saved[f'default_{record.direction}_link'] = record
        return key, saved

    def save_navigation_default(self, table, direction, link=None):
        if direction not in ('child', 'parent'):
            raise ValueError('Unknown navigation direction.')
        ref = self.database_backend.resolve(table)
        key = (self.model, ref.schema, ref.name)
        if link is None:
            self.store.set(*key, 'default_direction', direction)
        else:
            owner = link.relationship.child_table if direction == 'parent' else link.relationship.parent_table
            identity = self.dialect.identifier_key
            if (direction != link.direction or identity(owner.schema) != identity(ref.schema)
                    or identity(owner.name) != identity(ref.name)):
                raise ValueError('Selected relationship does not match this table and direction.')
            self.store.save_navigation_default(self.model, ref, link.selection, dialect=self.dialect)

    def _validate(self, columns, selected, order):
        for names in (selected, tuple(name for name, _ in order)):
            if len(names) != len(set(names)):
                raise ValueError('Choose each column only once.')
            for name in names:
                if name not in columns:
                    raise ValueError(f'Column {name!r} no longer exists. Use “Choose columns…” or “Set sort order…”.')
                self.dialect.validate_identifier(name)
        if any(direction not in ('ASC', 'DESC') for _, direction in order):
            raise ValueError('Sort direction must be ASC or DESC.')

    def save_preference(self, link, property_name, choices):
        if property_name not in ('columns', 'order_by'):
            raise ValueError('Unsupported table preference.')
        generation = self.metadata.generation
        ref = self._destination(link, generation)
        if choices is None:
            choices = ()
        else:
            if isinstance(choices, str):
                raise ValueError('Supply column choices as a sequence.')
            choices = tuple(choices)
            if not choices:
                raise ValueError('Choose at least one column.')
            selected, order = (choices, ()) if property_name == 'columns' else ((), choices)
            self._validate(self._columns(ref, generation), selected, order)
        if property_name == 'columns':
            self.store.set_selected_columns(self.model, ref, choices)
        else:
            self.store.set_sort_order(self.model, ref, choices)

    def _options(self, link):
        generation = self.metadata.generation
        return self._table_options(self._destination(link, generation), generation)

    def _table_options(self, ref, generation):
        columns = self.store.selected_columns(self.model, ref)
        order = self.store.sort_order(self.model, ref)
        if columns or order:
            self._validate(self._columns(ref, generation), columns, order)
        return columns, order

    def table_query(self, ref, limit):
        self.dialect.validate_schema(ref.schema)
        self.dialect.validate_identifier(ref.name)
        table = self.metadata.table(self.database_backend, ref, self.metadata.generation)
        selected, order = self._table_options(table.ref, self.metadata.generation) if self.model else ((), ())
        return QueryGenerator(self.dialect).query_for_table(
            table, columns=selected, order=order, limit=limit)

    def navigation_query(self, link, row, limit):
        selected, order = self._options(link) if self.model else ((), ())
        source = link.relationship.child_table if link.direction == 'parent' else link.relationship.parent_table
        return QueryGenerator(self.dialect).query_for_relationship(
            source, row, link, columns=selected, order=order, limit=limit)

    def row_navigation_query(self, link, row, limit):
        selected, order = self._options(link) if self.model else ((), ())
        table = self.metadata.table(self.database_backend, link.ref, self.metadata.generation)
        return QueryGenerator(self.dialect).query_for_primary_key(
            table, row, table.primary_key, columns=selected, order=order, limit=limit)
