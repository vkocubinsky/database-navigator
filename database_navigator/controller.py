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


__all__ = ["NavigationController", "RelationshipEditorController", "TablePreferenceEditorController"]


class NavigationController:
    def __init__(self, navigation: Navigation, state: BrowsingState, *,
                 before_metadata_reset=lambda: None, metadata_reset=lambda visit: None):
        self.navigation = navigation
        self.state = state
        self.tasks = PendingTasks()
        self._before_metadata_reset = before_metadata_reset
        self._metadata_reset = metadata_reset
        self._preference_request = 0

    @property
    def visit(self):
        return self.state.visit

    @property
    def current_table(self):
        return self.navigation.base_table(self.visit.query) if self.visit else None

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
        self._before_metadata_reset = self._metadata_reset = None

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
        self.tasks.cancel_reads()

    def change_selection(self):
        self.state.selection_revision += 1
        self.tasks.cancel_reads(stale_only=True)

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
        if self.closed:
            return
        self._before_metadata_reset()
        self.navigation.reset_metadata()
        for visit in self.state.history:
            if visit.links and visit.related_index is not None:
                visit.restore_link = visit.links[visit.related_index]
            elif visit.links == []:
                visit.related_index = 0
            visit.links = None
        self._metadata_reset(self.visit)

    def relationship_editor(self):
        """Open a model-scoped editor, initially pointing at the current visit."""
        return RelationshipEditorController(self)

    def table_preference_editor(self, link, name, *, current, refresh):
        """Bind a Columns or Sort order editor to its original relationship pane."""
        return TablePreferenceEditorController(self, link, name, current=current, refresh=refresh)

    def load_startup_sql(self, *, completed, failed):
        self.queue_ordered(self.navigation.startup_sql, (), completed, failed)

    def save_startup_sql(self, text, *, completed, failed):
        self.queue_ordered(self.navigation.save_startup_sql, (text,), completed, failed)

    def load_diagram(self, *, completed, failed, current):
        """Deliver the current visit's diagram while its relationship snapshot is valid."""
        visit = self.visit
        if visit is None or visit.links is None:
            return
        links = visit.links
        active = self._navigation_current(
            lambda: self.visit is visit and visit.links is links and current(), selection=False)
        self.submit(self.navigation.relationship_diagram, (self.current_table,), completed, failed, active)

    def diagram_table_key(self, schema, table):
        """Identify the same diagram node across different relationship endpoints."""
        return self.navigation.table_key(schema, table)

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

    def validate_row_key(self, key, row):
        return self.navigation.validate_row_key(key, row)

    def _open_current(self, view_current, *, selection=True):
        self.state.open_revision += 1
        revision = self.state.open_revision
        navigation_current = self._navigation_current(view_current, selection=selection)
        return lambda: revision == self.state.open_revision and navigation_current()

    def list_tables(self, *, completed, failed, current):
        """Load the chooser's catalog after pending preference operations."""
        self.queue_ordered(self.navigation.list_tables, (), completed, failed, current)

    def _accept_open(self, query, limit, before_open, opened, failed):
        """Commit the prepared query and its original limit together."""
        try:
            self.navigation.base_table(query)
            before_open()
            visit = self.add_visit(query, limit)
        except ValueError as exc:
            failed(exc)
            return
        opened(visit)

    def open_table(self, ref, limit, *, current, before_open, opened, failed):
        """Prepare after pending saves and add one visit if the chooser is still current.

        Closing or reloading the chooser silently retires its request. An open
        chooser gets a retryable error if another navigation action wins.
        before_open checks editor inputs and captures the departing view.
        opened receives the new visit on the polling thread.
        """
        latest = self._open_current(lambda: True, selection=False)

        def prepared(query):
            if not latest():
                failed(ValueError('Open cancelled because navigation changed. Choose Open to try again.'))
                return
            self._accept_open(query, limit, before_open, opened, failed)

        self.queue_ordered(self.navigation.table_query, (ref, limit), prepared, failed, current)

    def open_link(self, link, row, limit, *, by_primary_key=False, current, before_open, opened, failed):
        """Open the linked table or one row as a visit while its selection is current."""
        prepare = self.open_row if by_primary_key else self.open_related

        def prepared(query):
            self._accept_open(query, limit, before_open, opened, failed)

        return prepare(link, row, limit, prepared=prepared, failed=failed, current=current)

    def open_related(self, link, row, limit, *, prepared, failed, current):
        """Prepare table navigation; only the latest open request may be delivered."""
        return self.submit(self.navigation.navigation_query, (link, row, limit),
                           prepared, failed, self._open_current(current))

    def open_row(self, link, row, limit, *, prepared, failed, current):
        """Prepare primary-key navigation, preserving bound query values."""
        return self.submit(self.navigation.row_navigation_query, (link, row, limit),
                           prepared, failed, self._open_current(current))


class TablePreferenceEditorController:
    """Load and save one table setting, independently of subsequent browsing."""

    def __init__(self, browser: NavigationController, link, name, *, current, refresh):
        self._browser = browser
        self._link = link
        self._name = name
        browser._preference_request += 1
        self._request = browser._preference_request
        self._pane_current = browser._navigation_current(current, selection=False)
        self._refresh = refresh

    def load(self, *, completed, failed):
        def current():
            return self._request == self._browser._preference_request and self._pane_current()

        def loaded(result):
            columns, settings = result
            completed(columns, settings.get(self._name), settings.get(f'{self._name}_error', ''))

        self._browser.queue_ordered(self._browser.navigation.table_preferences, (self._link,),
                                    loaded, failed, current)

    def save(self, choices, *, completed, failed, current):
        """Persist the original target. Only a live dialog receives the outcome."""
        def saved(_):
            if current():
                completed()
            if self._pane_current():
                self._refresh()

        def error(exc):
            if current():
                failed(exc)

        self._browser.queue_ordered(self._browser.navigation.save_preference,
                                    (self._link, self._name, choices), saved, error)


class RelationshipEditorController:
    """Ordered editor operations and their effects on the browsing window."""

    def __init__(self, browser: NavigationController):
        self._browser = browser
        self.model = browser.navigation.model
        self.supports_schemas = browser.navigation.supports_schemas
        visit = browser.visit
        self.child = browser.navigation.initial_child_table(visit.query if visit else None)

    def list_relationships(self, *, completed, failed, current):
        self._browser.queue_ordered(self._browser.navigation.custom_relationships, (),
                                    completed, failed, current)

    def load_columns(self, child, parent, *, completed, failed, current):
        self._browser.queue_ordered(self._browser.navigation.relationship_endpoints, (child, parent),
                                    completed, failed, current)

    def _change(self, operation, args, completed, failed, current):
        # Once accepted, a write belongs to the window. Refresh even if its
        # editor was destroyed while the worker was committing the change.
        def saved(result):
            self._browser.reset_metadata()
            if current():
                completed(result)

        def error(exc):
            if current():
                failed(exc)

        self._browser.queue_ordered(operation, args, saved, error)

    def save(self, identifier, relationship, *, completed, failed, current):
        self._change(self._browser.navigation.save_custom_relationship, (identifier, relationship),
                     completed, failed, current)

    def delete(self, identifier, *, completed, failed, current):
        self._change(self._browser.navigation.delete_custom_relationship, (identifier,),
                     completed, failed, current)
