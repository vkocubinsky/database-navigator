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

"""Application service: explicit asynchronous database operations and pure helpers.

Only this boundary assembles browser sessions, preferences, caching and workers.
Every method returning a Future performs its database work on a pool worker.
"""

from concurrent.futures import Future

from .contracts import DatabaseBackend
from .metadata import MetadataCache
from .postgres import PostgreSQLBackend
from .preferences import PreferenceSession, validate_configuration
from .sqlite import SQLiteBackend
from .storage import DEFAULT_DATABASE
from .worker import DatabasePool


class NavigationService:
    def __init__(self, browser: DatabaseBackend, *, model=None, application_database=DEFAULT_DATABASE):
        if model is not None:
            validate_configuration(browser, model, application_database)
        self._browser = browser
        self.model = model
        self.label = browser.label
        self.supports_schemas = browser.dialect.supports_schemas
        self._metadata = MetadataCache()
        factory = browser.new_session
        if model:
            factory = lambda: PreferenceSession(browser.new_session(), model, application_database,
                                                metadata=self._metadata)
        self._pool = DatabasePool(factory, metadata=self._metadata)

    def close(self):
        """Cancel queued work without waiting; workers close their own connections."""
        self._pool.close()

    def reset_metadata(self):
        self._metadata.reset()

    def base_table(self, statement):
        """Pure parsing, with no connection or metadata lookup."""
        return self._browser.base_table(statement)

    def relationship_source(self, query=None):
        if query is None:
            return self._browser.dialect.default_schema, ''
        table = self.base_table(query.statement)
        return self._browser.dialect.split_reference(table)

    def table_key(self, schema, table):
        """Pure identity comparison for diagram nodes, including SQLite casing."""
        dialect = self._browser.dialect
        return (dialect.identifier_key(schema or dialect.default_schema),
                dialect.identifier_key(table))

    def validate_row_key(self, key, columns, row):
        return self._browser.dialect.validate_row_key(key, columns, row)

    def query(self, sql, limit, parameters=()) -> Future:
        # Preserve the minimal call signature for backend implementations.
        args = (sql, limit, parameters) if parameters else (sql, limit)
        return self._pool.submit('query', *args)

    def relationships(self, table) -> Future:
        return self._pool.submit('relationships', table)

    def relationship_diagram(self, table) -> Future:
        """Return master identity, direct links, and key/relationship columns."""
        return self._pool.submit('relationship_diagram', table)

    def primary_key(self, table, schema=None) -> Future:
        return self._pool.submit('primary_key', table, schema)

    def navigation_query(self, link, columns, row, limit) -> Future:
        return self._pool.submit('navigation_query', link, columns, row, limit)

    def row_navigation_query(self, link, columns, row, limit) -> Future:
        return self._pool.submit('row_navigation_query', link, columns, row, limit)

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

    def save_custom_relationship(self, identifier, document) -> Future:
        return self._pool.submit('save_custom_relationship', identifier, document)

    def delete_custom_relationship(self, identifier) -> Future:
        return self._pool.submit('delete_custom_relationship', identifier)


def create_backend(connection_string) -> DatabaseBackend:
    """Select a read-only database backend without opening a connection."""
    backend_type = SQLiteBackend if connection_string.startswith("sqlite:") else PostgreSQLBackend
    return backend_type(connection_string)


def open_service(connection, *, model=None, application_database=DEFAULT_DATABASE):
    """Validate configuration before starting any workers or opening the window."""
    return NavigationService(create_backend(connection), model=model,
                             application_database=application_database)
