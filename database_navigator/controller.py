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

"""Glue between UI-owned browsing state and asynchronous backend operations.

The owning thread calls poll(); all result callbacks run on that same thread.
Callbacks receive data, never workers or database connections. Views may add
validity predicates for focus, editor revisions and the lifetime of a dialog.
"""

from .navigation import Navigation, Query, default_link_index, validate_limit
from .ui_state import BrowsingState, Visit, NavigationDefaults
from .worker import PendingTasks


__all__ = ["NavigationController"]


class NavigationController:
    def __init__(self, navigation: Navigation, state: BrowsingState):
        self.navigation = navigation
        self.state = state
        self.tasks = PendingTasks()

    @property
    def visit(self):
        return self.state.visit

    @property
    def closed(self):
        return self.tasks.closed

    @property
    def busy(self):
        return self.tasks.busy

    def submit(self, operation, args, completed, failed, current=lambda: True):
        return self.tasks.submit(operation, args, completed, failed, current)

    def queue_ordered(self, operation, args, completed, failed, current=lambda: True):
        self.tasks.queue_ordered(operation, args, completed, failed, current)

    def poll(self):
        self.tasks.poll()

    def close(self):
        self.tasks.close()
        self.navigation.close()

    def query_for_text(self, text, navigation=None):
        if navigation is not None:
            query = navigation
        else:
            if not text.strip():
                raise ValueError('Enter a SELECT query before pressing Send.')
            previous = self.visit.query if self.visit else None
            # An unchanged generated query retains bound values on Send.
            query = previous if previous and previous.text == text else Query(text, text)
        self.navigation.base_table(query)
        return query

    def add_visit(self, query, limit):
        validate_limit(limit)
        del self.state.history[self.state.history_index + 1:]
        visit = Visit(query, limit)
        self.state.history.append(visit)
        self.state.history_index = len(self.state.history) - 1
        return visit

    def can_move(self, offset):
        return 0 <= self.state.history_index + offset < len(self.state.history)

    def move_history(self, offset):
        if not self.can_move(offset):
            return None
        self.state.history_index += offset
        return self.visit

    def capture_visit(self, *, search, direction, selected_index, related_index,
                      links_ready, limit=None):
        visit = self.visit
        if visit is None:
            return
        visit.search, visit.direction = search, direction
        if visit.rows is not None:
            visit.selected_index = selected_index
        if links_ready:
            visit.related_index = related_index
        if limit is not None:
            visit.limit = limit

    def begin_visit(self):
        self.state.request_revision += 1
        self.state.selection_revision += 1
        for future, *_ in self.tasks.pending:
            future.cancel()

    def change_selection(self):
        self.state.selection_revision += 1
        for future, _, _, current in self.tasks.pending:
            if not current():
                future.cancel()

    def _navigation_current(self, view_current, *, selection=True):
        """Combine UI-owned navigation validity with view lifetime checks."""
        request, selected = self.state.request_revision, self.state.selection_revision
        return lambda: (not self.closed and request == self.state.request_revision
                        and (not selection or selected == self.state.selection_revision)
                        and view_current())

    def set_filters(self, *, search=None, direction=None):
        if self.visit is not None:
            if search is not None:
                self.visit.search = search
            if direction is not None:
                self.visit.direction = direction

    def reset_metadata(self):
        self.navigation.reset_metadata()
        for visit in self.state.history:
            if visit.links and visit.related_index is not None:
                visit.restore_link = visit.links[visit.related_index]
            elif visit.links == []:
                visit.related_index = 0
            visit.links = None

    @staticmethod
    def visible_links(links, search, direction):
        pattern = search.casefold()
        return [index for index, link in enumerate(links)
                if pattern in link.table.casefold() and direction.lower() == link.direction]

    def preferred_link(self, links, search, direction):
        saved = (self.visit.defaults or {}).get(f'default_{direction.lower()}_link')
        index = default_link_index(links, saved)
        visible = self.visible_links(links, search, direction)
        candidate = index if index is not None else next(iter(visible), None)
        return candidate if candidate in visible else None

    def load_visit(self, visit, *, rows_ready, links_ready, defaults_ready,
                   query_failed, metadata_failed, status):
        current = self._navigation_current(lambda: True, selection=False)
        table = self.navigation.base_table(visit.query)

        def metadata_ready(links):
            visit.links = links
            if not visit.defaults_applied:
                defaults = visit.defaults or {}
                if defaults.get('default_direction') not in ('parent', 'child'):
                    visit.direction = 'Child' if any(link.direction == 'child' for link in links) else 'Parent'
                    defaults_ready(visit.direction)
                saved = defaults.get(f'default_{visit.direction.lower()}_link')
                index = default_link_index(links, saved)
                visit.related_index = index if index is not None else 0
                visit.defaults_applied = True
            if visit.restore_link is not None:
                index = default_link_index(links, visit.restore_link.selection)
                visit.related_index = index if index is not None else 0
                visit.restore_link = None
            # Filters may change while discovery is running.
            if links and visit.related_index is not None:
                index = min(visit.related_index, len(links) - 1)
                visible = self.visible_links(links, visit.search, visit.direction)
                visit.related_index = index if index in visible else next(iter(visible), None)
            else:
                visit.related_index = None
            links_ready(links)

        def discover():
            if not current():
                return
            if visit.links is not None:
                metadata_ready(visit.links)
            else:
                status(f'{len(visit.rows)} rows fetched. Discovering relationships for {table}…')
                self.submit(self.navigation.navigation_links, (table,), metadata_ready, metadata_failed, current)

        def loaded_defaults(result):
            key, saved = result
            shared = self.state.defaults.setdefault(key, NavigationDefaults(saved))
            shared.refresh(saved)
            visit.defaults = shared
            direction = shared.get('default_direction')
            if direction in ('parent', 'child'):
                visit.direction = direction.title()
                defaults_ready(visit.direction)
            discover()

        def loaded_rows(result):
            visit.columns, visit.rows = result
            rows_ready(result)
            if not current() or not visit.rows:
                return
            if self.navigation.model and visit.defaults is None:
                status(f'Loading navigation defaults for {table}…')
                self.queue_ordered(self.navigation.navigation_defaults, (table,),
                                   loaded_defaults, metadata_failed, current)
            else:
                discover()

        def failed(exc):
            visit.error = str(exc)
            query_failed(exc)

        if visit.rows is not None:
            loaded_rows((visit.columns, visit.rows))
        elif visit.error:
            query_failed(visit.error)
        else:
            status(f'Executing query for {table}…')
            self.submit(self.navigation.query, (visit.query.statement, visit.limit, visit.query.parameters),
                        loaded_rows, failed, current)

    def save_navigation_default(self, direction, link, failed):
        visit = self.visit
        if not self.navigation.model or visit is None or visit.defaults is None:
            return
        direction = direction.lower()
        name = f'default_{direction}_link' if link is not None else 'default_direction'
        value = link.selection if link is not None else direction
        defaults = visit.defaults
        token = defaults.stage({name: value, 'default_direction': direction})

        def completed(_):
            defaults.complete(token, succeeded=True)

        def error(exc):
            defaults.complete(token, succeeded=False)
            failed(exc)

        table = self.navigation.base_table(visit.query)
        self.queue_ordered(self.navigation.save_navigation_default, (table, direction, link), completed, error)

    def load_related(self, link, row, limit, *, prepared, completed, failed, current):
        current = self._navigation_current(current)

        def ready(query):
            prepared(query)
            if current():
                self.submit(self.navigation.execute_related_query, (query, limit), completed, failed, current)

        return self.submit(self.navigation.navigation_query, (link, row, limit), ready, failed, current)

    def load_primary_key(self, link, *, completed, failed, current):
        # Metadata belongs to the displayed table, independently of its selected row.
        current = self._navigation_current(current, selection=False)
        return self.submit(self.navigation.primary_key, (link.table, link.schema), completed, failed, current)

    def _prepare_open(self, operation, args, prepared, failed, view_current):
        self.state.open_revision += 1
        revision = self.state.open_revision
        navigation_current = self._navigation_current(view_current)
        current = lambda: revision == self.state.open_revision and navigation_current()
        return self.submit(operation, args, prepared, failed, current)

    def open_related(self, link, row, limit, *, prepared, failed, current):
        """Prepare table navigation; only the latest open request may be delivered."""
        return self._prepare_open(self.navigation.navigation_query, (link, row, limit),
                                  prepared, failed, current)

    def open_row(self, link, row, limit, *, prepared, failed, current):
        """Prepare primary-key navigation, preserving bound query values."""
        return self._prepare_open(self.navigation.row_navigation_query, (link, row, limit),
                                  prepared, failed, current)
