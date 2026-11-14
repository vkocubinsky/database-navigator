"""Tk-independent navigation state and asynchronous workflow coordination.

The owning thread calls poll(); all result callbacks run on that same thread.
Callbacks receive data, never workers or database connections. Views may add
validity predicates for focus, editor revisions and the lifetime of a dialog.
"""

from collections import deque
from dataclasses import dataclass
import json

from .models import (NavigationDefaults, Query, Relationship, default_link_index,
                     link_identity, validate_limit)
from .service import NavigationService


@dataclass
class Visit:
    query: Query
    limit: int
    columns: tuple = ()
    rows: list | None = None
    links: list | None = None
    selected_index: int | None = 0
    related_index: int | None = 0
    search: str = ''
    direction: str = 'Child'
    error: str | None = None
    restore_link: Relationship | None = None
    defaults: NavigationDefaults | None = None
    defaults_applied: bool = False


class NavigationController:
    def __init__(self, service: NavigationService):
        self.service = service
        self.history = []
        self.history_index = -1
        self._request_revision = 0
        self._selection_revision = 0
        self._open_revision = 0
        self.closed = False
        self.pending = []
        self._ordered_jobs = deque()
        self._ordered_job = None
        self._defaults = {}

    @property
    def visit(self):
        return self.history[self.history_index] if self.history_index >= 0 else None

    @property
    def busy(self):
        return (bool(self._ordered_jobs) or self._ordered_job is not None
                or any(current() for _, _, _, current in self.pending))

    def submit(self, operation, args, completed, failed, current=lambda: True):
        future = operation(*args)
        self.pending.append((future, completed, failed, current))
        return future

    def queue_ordered(self, operation, args, completed, failed, current=lambda: True):
        # Saves outlive navigation. New defaults reads must follow earlier saves.
        self._ordered_jobs.append((operation, args, completed, failed, current))

    @staticmethod
    def _deliver(future, completed, failed):
        try:
            result = future.result()
        except Exception as exc:
            failed(exc)
        else:
            completed(result)

    def _poll_ordered(self):
        if self._ordered_job is not None:
            future, completed, failed, current = self._ordered_job
            if not future.done():
                return
            self._ordered_job = None
            if current():
                self._deliver(future, completed, failed)
        while not self.closed and self._ordered_jobs and self._ordered_job is None:
            operation, args, completed, failed, current = self._ordered_jobs.popleft()
            if current():
                self._ordered_job = (operation(*args), completed, failed, current)

    def poll(self):
        if self.closed:
            return
        self._poll_ordered()
        pending, self.pending = self.pending, []
        for future, completed, failed, current in pending:
            if self.closed or not current():
                future.cancel()
            elif not future.done():
                self.pending.append((future, completed, failed, current))
            else:
                self._deliver(future, completed, failed)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for future, *_ in self.pending:
            future.cancel()
        self.pending.clear()
        self._ordered_jobs.clear()
        if self._ordered_job is not None:
            self._ordered_job[0].cancel()
            self._ordered_job = None
        self.service.close()

    def query_for_text(self, text, navigation=None):
        if navigation is not None:
            query = navigation
        else:
            if not text.strip():
                raise ValueError('Enter a SELECT query before pressing Send.')
            previous = self.visit.query if self.visit else None
            # An unchanged generated query retains bound values on Send.
            query = previous if previous and previous.text == text else Query(text, text)
        self.service.base_table(query.statement)
        return query

    def add_visit(self, query, limit):
        validate_limit(limit)
        del self.history[self.history_index + 1:]
        visit = Visit(query, limit)
        self.history.append(visit)
        self.history_index = len(self.history) - 1
        return visit

    def can_move(self, offset):
        return 0 <= self.history_index + offset < len(self.history)

    def move_history(self, offset):
        if not self.can_move(offset):
            return None
        self.history_index += offset
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
        self._request_revision += 1
        self._selection_revision += 1
        for future, *_ in self.pending:
            future.cancel()

    def change_selection(self):
        self._selection_revision += 1
        for future, _, _, current in self.pending:
            if not current():
                future.cancel()

    def _navigation_current(self, view_current, *, selection=True):
        """Combine controller-owned navigation validity with view lifetime checks."""
        request, selected = self._request_revision, self._selection_revision
        return lambda: (not self.closed and request == self._request_revision
                        and (not selection or selected == self._selection_revision)
                        and view_current())

    def set_filters(self, *, search=None, direction=None):
        if self.visit is not None:
            if search is not None:
                self.visit.search = search
            if direction is not None:
                self.visit.direction = direction

    def reset_metadata(self):
        self.service.reset_metadata()
        for visit in self.history:
            if visit.links and visit.related_index is not None:
                visit.restore_link = visit.links[visit.related_index]
            elif visit.links == []:
                visit.related_index = 0
            visit.links = None

    @staticmethod
    def visible_links(links, search, direction):
        pattern = search.casefold()
        return [index for index, link in enumerate(links)
                if pattern in link.table.casefold() and direction.lower() in ('any', link.direction)]

    def preferred_link(self, links, search, direction):
        saved = (self.visit.defaults or {}).get(f'default_{direction.lower()}_link')
        index = default_link_index(links, saved)
        visible = self.visible_links(links, search, direction)
        candidate = index if index is not None else next(iter(visible), None)
        return candidate if candidate in visible else None

    def load_visit(self, visit, *, rows_ready, links_ready, defaults_ready,
                   query_failed, metadata_failed, status):
        current = self._navigation_current(lambda: True, selection=False)
        table = self.service.base_table(visit.query.statement)

        def metadata_ready(links):
            visit.links = links
            if self.service.model and not visit.defaults_applied:
                saved = (visit.defaults or {}).get(f'default_{visit.direction.lower()}_link')
                index = default_link_index(links, saved)
                visit.related_index = index if index is not None else 0
                visit.defaults_applied = True
            if visit.restore_link is not None:
                visit.related_index = next((i for i, link in enumerate(links)
                                            if link == visit.restore_link), 0)
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
                self.submit(self.service.relationships, (table,), metadata_ready, metadata_failed, current)

        def loaded_defaults(result):
            key, saved = result
            shared = self._defaults.setdefault(key, NavigationDefaults(saved))
            shared.refresh(saved)
            visit.defaults = shared
            direction = saved.get('default_direction', 'child')
            visit.direction = direction.title() if direction in ('child', 'parent', 'any') else 'Child'
            defaults_ready(visit.direction)
            discover()

        def loaded_rows(result):
            visit.columns, visit.rows = result
            rows_ready(result)
            if not current() or not visit.rows:
                return
            if self.service.model and visit.defaults is None:
                status(f'Loading navigation defaults for {table}…')
                self.queue_ordered(self.service.navigation_defaults, (table,),
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
            self.submit(self.service.query, (visit.query.statement, visit.limit, visit.query.parameters),
                        loaded_rows, failed, current)

    def save_navigation_default(self, direction, link, failed):
        visit = self.visit
        if not self.service.model or visit is None or visit.defaults is None:
            return
        direction = direction.lower()
        name = f'default_{direction}_link' if link is not None else 'default_direction'
        value = json.dumps(link_identity(link)) if link is not None else direction
        defaults = visit.defaults
        token = defaults.stage({name: value, 'default_direction': direction})

        def completed(_):
            defaults.complete(token, succeeded=True)

        def error(exc):
            defaults.complete(token, succeeded=False)
            failed(exc)

        table = self.service.base_table(visit.query.statement)
        self.queue_ordered(self.service.save_navigation_default, (table, direction, link), completed, error)

    def load_related(self, link, columns, row, limit, *, prepared, completed, failed, current):
        current = self._navigation_current(current)

        def ready(query):
            prepared(query)
            if current():
                self.submit(self.service.execute_related_query, (query, limit), completed, failed, current)

        return self.submit(self.service.navigation_query, (link, columns, row, limit), ready, failed, current)

    def load_primary_key(self, link, *, completed, failed, current):
        # Metadata belongs to the displayed table, independently of its selected row.
        current = self._navigation_current(current, selection=False)
        return self.submit(self.service.primary_key, (link.table, link.schema), completed, failed, current)

    def _prepare_open(self, operation, args, prepared, failed, view_current):
        self._open_revision += 1
        revision = self._open_revision
        navigation_current = self._navigation_current(view_current)
        current = lambda: revision == self._open_revision and navigation_current()
        return self.submit(operation, args, prepared, failed, current)

    def open_related(self, link, columns, row, limit, *, prepared, failed, current):
        """Prepare table navigation; only the latest open request may be delivered."""
        return self._prepare_open(self.service.navigation_query, (link, columns, row, limit),
                                  prepared, failed, current)

    def open_row(self, link, columns, row, limit, *, prepared, failed, current):
        """Prepare primary-key navigation, preserving the service's bound query."""
        return self._prepare_open(self.service.row_navigation_query, (link, columns, row, limit),
                                  prepared, failed, current)
