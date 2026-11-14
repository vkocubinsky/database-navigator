"""Shared data and pure validation; no database connections or UI dependencies."""

from dataclasses import dataclass
import json
import re

DEFAULT_LIMIT = 50


class TableNotFoundError(LookupError):
    """The requested table does not exist in the selected schema/search path."""


@dataclass(frozen=True)
class ForeignKey:
    name: str | None
    source_schema: str
    source_table: str
    source_columns: tuple[str, ...]
    target_schema: str
    target_table: str
    target_columns: tuple[str, ...]


@dataclass(frozen=True)
class Query:
    text: str
    statement: str
    parameters: tuple = ()


@dataclass(frozen=True)
class Relationship:
    table: str
    columns: tuple[str, ...]
    selected_columns: tuple[str, ...]
    direction: str
    schema: str | None = None

    @property
    def display_table(self):
        # Labels are not SQL; display even names rejected by query generation.
        return f'{self.schema}.{self.table}' if self.schema else self.table

    def notation(self, selected_table):
        """Keep the linked table first; point from foreign key to referenced key."""
        arrow = '←' if self.direction == 'parent' else '→'
        return (f"{self.display_table}({', '.join(self.columns)}) {arrow} "
                f"{selected_table}({', '.join(self.selected_columns)})")


_NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'


def link_identity(link):
    """Stable identity independent of metadata ordering or constraint names."""
    return {'schema': link.schema or 'main', 'table': link.table,
            'direction': link.direction,
            'column_pairs': [{'main': main, 'linked': linked}
                             for main, linked in zip(link.selected_columns, link.columns)]}


def default_link_index(links, value):
    """Missing, malformed, or obsolete saved links simply use the UI fallback."""
    try:
        identity = json.loads(value)
    except (TypeError, ValueError):
        return None
    return next((index for index, link in enumerate(links) if link_identity(link) == identity), None)


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


def parse_columns(value, *, ordering=False):
    """Parse only identifier lists with optional ASC/DESC, never expressions."""
    if value is None:
        return ()
    result = []
    remaining = value
    pattern = rf'\s*({_NAME})' + (r'(?:\s+(ASC|DESC))?' if ordering else '') + r'\s*(,|$)'
    while remaining:
        match = re.match(pattern, remaining, re.IGNORECASE)
        if match is None:
            raise ValueError('Invalid saved column list. Choose columns again or reset to the default.')
        name = match[1]
        name = name[1:-1].replace('""', '"') if name.startswith('"') else name
        result.append((name, (match[2] or 'ASC').upper()) if ordering else name)
        remaining = remaining[match.end():]
        if match[3 if ordering else 2] == ',' and not remaining.strip():
            raise ValueError('A column list cannot end with a comma.')
    if not result:
        raise ValueError('Choose at least one column or reset to the default.')
    return tuple(result)


def validate_limit(limit):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError("Row limit must be an integer between 1 and 10000.")


def stored_column_name(name):
    """Encode a name in the local preference format, not in a SQL dialect."""
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', name):
        return name
    return '"' + name.replace('"', '""') + '"'
