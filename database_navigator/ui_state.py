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

"""UI-owned browsing state, usable without creating Tk widgets."""

from dataclasses import dataclass, field
from .navigation import Query, NavigationLink


__all__ = ["BrowsingState", "Visit", "NavigationDefaults"]


class NavigationDefaults:
    """Main-thread view of confirmed preferences plus ordered pending saves."""

    def __init__(self, saved):
        self._saved = dict(saved)
        self._pending = {}

    def refresh(self, saved):
        self._saved = dict(saved)

    def get(self, name, default=None):
        for updates in reversed(tuple(self._pending.values())):
            if name in updates:
                return updates[name]
        return self._saved.get(name, default)

    def stage(self, updates):
        token = object()
        self._pending[token] = dict(updates)
        return token

    def complete(self, token, *, succeeded):
        updates = self._pending.pop(token)
        if succeeded:
            self._saved.update(updates)


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
    direction: str = 'Parent'
    error: str | None = None
    restore_link: NavigationLink | None = None
    defaults: NavigationDefaults | None = None
    defaults_applied: bool = False


@dataclass
class BrowsingState:
    history: list[Visit] = field(default_factory=list)
    history_index: int = -1
    request_revision: int = 0
    selection_revision: int = 0
    open_revision: int = 0
    defaults: dict = field(default_factory=dict)

    @property
    def visit(self):
        return self.history[self.history_index] if self.history_index >= 0 else None
