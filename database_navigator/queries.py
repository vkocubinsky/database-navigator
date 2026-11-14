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

"""Pure SQL parsing, row-key matching, and query assembly shared by adapters."""

import re

from .models import Query, validate_limit


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


def derived_reference(query, *, postgres=False):
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
    return parse_table(reference, postgres=postgres)


def relationship_values(relationship, columns, row, dialect):
    return column_values(relationship.selected_columns, columns, row, dialect.identifier_key)


def related_query(table, columns, values, limit, placeholder, projection='*', order=''):
    """Build the bound statement for a prepared navigation query."""
    validate_limit(limit)
    predicates = ' AND '.join(f'{name} = {placeholder}' for name in columns)
    return (f'SELECT {projection} FROM {table} WHERE {predicates}{order} LIMIT {placeholder}',
            (*values, limit))


def navigation_query(table, columns, values, limit, placeholder, literal, projection='*', order=''):
    validate_limit(limit)
    display = ' and '.join(f'{name} = {literal(value)}' for name, value in zip(columns, values))
    preview = f'select {projection}\nfrom {table}\nwhere {display}'
    if order:
        preview += '\n' + order.lstrip()
    preview += f'\nlimit {limit};'
    statement, parameters = related_query(table, columns, values, limit, placeholder, projection, order)
    return Query(preview, statement, parameters)


def selected_sql(selected_columns=None, order_by=(), *, identifier):
    """Compile structured column choices, never raw SQL fragments."""
    projection = ', '.join(map(identifier, selected_columns)) if selected_columns else '*'
    terms = []
    for name, direction in order_by:
        if direction not in ('ASC', 'DESC'):
            raise ValueError('Sort direction must be ASC or DESC.')
        terms.append(f'{identifier(name)} {direction}')
    return projection, ' order by ' + ', '.join(terms) if terms else ''
