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

"""Per-window immutable metadata snapshots shared by worker-owned sessions."""

from threading import Lock

from .metadata_models import TableRef


__all__ = ["MetadataCache"]


class MetadataCache:
    def __init__(self):
        self._lock = Lock()
        self._generation = 0
        self._aliases = {}
        self._table_list = {}
        self._tables = {}
        self._details = {}

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def reset(self):
        with self._lock:
            self._generation += 1
            self._aliases.clear()
            self._table_list.clear()
            self._tables.clear()
            self._details.clear()

    def _read(self, cache, key, generation, discover):
        with self._lock:
            value = cache.get(key) if generation == self._generation else None
        if value is None:
            # Never hold the shared cache lock during database I/O.
            value = discover()
            with self._lock:
                if generation == self._generation:
                    cache[key] = value
        return value

    @staticmethod
    def _key(session, ref):
        identity = session.dialect.identifier_key
        return TableRef(identity(ref.schema), identity(ref.name))

    def list_tables(self, session, generation):
        return self._read(self._table_list, None, generation, session.list_tables)

    def resolve_table(self, session, table, generation):
        return self._read(self._aliases, table, generation, lambda: session.resolve(table))

    def table(self, session, ref, generation):
        return self._read(self._tables, self._key(session, ref), generation,
                          lambda: session.table(ref))

    def details(self, session, ref, generation):
        key = self._key(session, ref)
        details = self._read(self._details, key, generation, lambda: session.details(ref))
        with self._lock:
            if generation == self._generation:
                self._tables[key] = details.table
        return details

    def navigation_links(self, session, table, generation):
        from .navigation import navigation_links

        ref = self.resolve_table(session, table, generation)
        details = self.details(session, ref, generation)
        return navigation_links(details, show_schema=session.dialect.supports_schemas)

    def relationship_diagram(self, session, table, generation):
        """Read tables for direct endpoints only, never their relationships."""
        ref = self.resolve_table(session, table, generation)
        links = self.navigation_links(session, table, generation)
        master = self._key(session, ref)
        endpoints = {master: ref}
        participants = {master: []}
        for link in links:
            key = self._key(session, link.ref)
            endpoints.setdefault(key, link.ref)
            participants.setdefault(key, []).extend(link.columns)
            participants[master].extend(link.selected_columns)
        columns = {}
        for key, endpoint in endpoints.items():
            primary = self.table(session, endpoint, generation).primary_key
            identity = session.dialect.identifier_key
            primary_names = {identity(column) for column in primary}
            seen, selected = set(), []
            for column in (*primary, *participants[key]):
                name = identity(column)
                if name not in seen:
                    selected.append((column, name in primary_names))
                    seen.add(name)
            columns[(key.schema, key.name)] = tuple(selected)
        return (ref.schema, ref.name), links, columns
