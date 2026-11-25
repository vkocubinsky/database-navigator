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

"""Query values, table references and row-key checks shared by navigation and adapters."""

from dataclasses import dataclass
import re

from .contracts import DatabaseDialect
from .metadata_models import TableRef


__all__ = [
    "Query", "validate_limit", "parse_table", "column_values", "primary_key_values",
    "derived_reference", "base_table_for_query",
]


@dataclass(frozen=True)
class Query:
    text: str
    statement: str
    parameters: tuple = ()
    table_ref: TableRef | None = None


def validate_limit(limit):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError("Row limit must be an integer between 1 and 10000.")


_IDENTIFIER = r'(?:"(?:[^"]|"")+"|\w[\w$]*)'
_TABLE = rf'{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?'


def parse_table(name, *, postgres=False):
    """Parse SQL notation; only PostgreSQL folds unquoted identifiers."""
    if not re.fullmatch(_TABLE, name):
        raise ValueError('Use table or schema.table, with double quotes for special names.')
    parts = re.findall(_IDENTIFIER, name)
    parts = [part[1:-1].replace('""', '"') if part.startswith('"')
             else part.lower() if postgres else part for part in parts]
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def column_values(names, columns, row, identifier_key):
    values = []
    for name in names:
        matches = [i for i, column in enumerate(columns) if identifier_key(column) == identifier_key(name)]
        if len(matches) != 1:
            raise ValueError(f"Query must return an unambiguous column named {name!r}.")
        values.append(row[matches[0]])
    return tuple(values)


def primary_key_values(key, columns, row, identifier_key):
    if not key:
        raise ValueError('This table has no declared primary key.')
    try:
        values = column_values(key, columns, row, identifier_key)
    except ValueError as exc:
        raise ValueError(f'{exc} Include all primary-key columns through Columns.') from None
    if any(value is None for value in values):
        raise ValueError('The selected row has a NULL primary-key value.')
    return values


def _editor_table_token(query):
    """Prefer an explicit override; otherwise infer from the first FROM table.

    This deliberately simple heuristic is not a SQL parser. Complex queries can
    supply -- derived: table_name. Never insert an annotation or change the SQL.
    """
    matches = re.findall(r"^[ \t]*--[ \t]*derived:([^\r\n]*)", query,
                         re.MULTILINE | re.IGNORECASE)
    if matches:
        if len(matches) != 1 or not matches[0].strip():
            raise ValueError("Use exactly one nonempty -- derived: table_name comment.")
        reference = matches[0].strip()
    else:
        match = re.search(rf'\bfrom\s+({_TABLE})', query, re.IGNORECASE)
        if match is None:
            raise ValueError("Cannot determine the base table. Add a -- derived: table_name comment.")
        reference = match[1]
    return reference


def derived_reference(query, *, postgres=False):
    return parse_table(_editor_table_token(query), postgres=postgres)


def base_table_for_query(query, dialect: DatabaseDialect):
    """Choose the browsing table without opening a connection or rewriting SQL."""
    if isinstance(query, Query):
        if query.table_ref is not None:
            return dialect.table_reference(query.table_ref.name, query.table_ref.schema)
        query = query.statement
    return dialect.reference_from_sql(_editor_table_token(query))
