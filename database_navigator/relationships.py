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

"""Optional, local relationship declarations; never change database metadata."""

from .models import ForeignKey, Relationship
from .contracts import DatabaseDialect


def merge_relationships(result, keys, schema, table, *, dialect: DatabaseDialect):
    """Append declared links in either direction, retaining discovered constraints."""
    identity = dialect.identifier_key

    def signature(link):
        return (identity(link.schema or dialect.default_schema), identity(link.table),
                tuple(map(identity, link.columns)),
                tuple(map(identity, link.selected_columns)), link.direction)

    seen = {signature(link) for link in result}
    selected = (identity(schema), identity(table))
    for key in keys:
        candidates = []
        if selected == (identity(key.source_schema), identity(key.source_table)):
            candidates.append(Relationship(key.target_table, key.target_columns, key.source_columns,
                                           'parent', dialect.relationship_schema(key.target_schema)))
        if selected == (identity(key.target_schema), identity(key.target_table)):
            candidates.append(Relationship(key.source_table, key.source_columns, key.target_columns,
                                           'child', dialect.relationship_schema(key.source_schema)))
        for link in candidates:
            if signature(link) not in seen:
                result.append(link)
                seen.add(signature(link))
    return result


def parse_relationships(document, *, dialect: DatabaseDialect):
    """Validate structured declarations from the relationship editor or local storage."""
    if not isinstance(document, dict) or set(document) != {'relationships'}:
        raise ValueError('Expected an object containing only "relationships".')
    if not isinstance(document['relationships'], list):
        raise ValueError('"relationships" must be an array.')
    keys = []
    for index, item in enumerate(document['relationships'], 1):
        if not isinstance(item, dict) or set(item) != {'source', 'target'}:
            raise ValueError(f'Relationship {index}: expected source and target.')
        endpoints = []
        for side in ('source', 'target'):
            endpoint = item[side]
            if not isinstance(endpoint, dict) or set(endpoint) != {'schema', 'table', 'columns'}:
                raise ValueError(f'Relationship {index} {side}: expected schema, table, columns.')
            schema, table, columns = (endpoint[k] for k in ('schema', 'table', 'columns'))
            if not isinstance(columns, list) or not columns:
                raise ValueError(f'Relationship {index}: columns must be a nonempty array.')
            for name in (schema, table, *columns):
                if not isinstance(name, str) or not name or '\0' in name:
                    raise ValueError(f'Relationship {index}: identifiers must be nonempty strings without NUL.')
                dialect.validate_identifier(name)
            dialect.validate_schema(schema)
            normalized = [dialect.identifier_key(name) for name in columns]
            if len(set(normalized)) != len(columns):
                raise ValueError(f'Relationship {index}: duplicate columns.')
            endpoints.append((schema, table, tuple(columns)))
        if len(endpoints[0][2]) != len(endpoints[1][2]):
            raise ValueError(f'Relationship {index}: source and target column counts must match.')
        keys.append(ForeignKey(None, *endpoints[0], *endpoints[1]))
    return tuple(keys)
