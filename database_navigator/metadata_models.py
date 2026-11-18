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

"""Immutable table metadata and structural relationships."""

from dataclasses import dataclass
from typing import Literal


__all__ = ["TableRef", "TableSummary", "Column", "Table", "Relationship", "TableDetails"]


def _identifier(name):
    if not isinstance(name, str) or not name or '\0' in name:
        raise ValueError('Identifiers must be nonempty strings without NUL.')


@dataclass(frozen=True)
class TableRef:
    schema: str
    name: str

    def __post_init__(self):
        _identifier(self.schema)
        _identifier(self.name)


@dataclass(frozen=True)
class TableSummary:
    """A catalog entry without column or relationship discovery."""

    ref: TableRef
    kind: Literal['table', 'view', 'materialized_view'] = 'table'

    def __post_init__(self):
        if self.kind not in ('table', 'view', 'materialized_view'):
            raise ValueError('Unknown table kind.')


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool = True

    def __post_init__(self):
        _identifier(self.name)


@dataclass(frozen=True)
class Table:
    ref: TableRef
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ()
    kind: Literal['table', 'view', 'materialized_view'] = 'table'

    def __post_init__(self):
        names = tuple(column.name for column in self.columns)
        if len(set(names)) != len(names):
            raise ValueError('Table columns must be distinct.')
        if len(set(self.primary_key)) != len(self.primary_key) or not set(self.primary_key) <= set(names):
            raise ValueError('Primary-key columns must be distinct columns of the table.')
        if self.kind not in ('table', 'view', 'materialized_view'):
            raise ValueError('Unknown table kind.')
        if self.kind != 'table' and self.primary_key:
            raise ValueError('Views have no declared primary key.')


@dataclass(frozen=True)
class Relationship:
    parent_table: TableRef
    child_table: TableRef
    parent_key: tuple[str, ...]
    child_key: tuple[str, ...]
    origin: Literal['database', 'user_defined']
    name: str | None = None
    relationship_id: int | None = None

    def __post_init__(self):
        if not self.child_key or len(self.child_key) != len(self.parent_key):
            raise ValueError('Child and parent keys must have the same nonzero number of columns.')
        for key in (self.child_key, self.parent_key):
            if not isinstance(key, tuple):
                raise ValueError('Relationship keys must be tuples.')
            for column in key:
                _identifier(column)
        if self.origin not in ('database', 'user_defined'):
            raise ValueError('Unknown relationship origin.')


@dataclass(frozen=True)
class TableDetails:
    """Relationships grouped by the role of the table reached from this table."""

    table: Table
    parent_relationships: tuple[Relationship, ...] = ()
    child_relationships: tuple[Relationship, ...] = ()
