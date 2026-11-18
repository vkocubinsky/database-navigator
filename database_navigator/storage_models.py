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

"""Immutable values persisted in the local application database."""

from dataclasses import dataclass
from typing import Literal

from .metadata_models import Relationship, TableRef


__all__ = ["StoredColumn", "StoredOrder", "StoredRelationship", "NavigationDefault"]


def _validate_column_position(model, schema_name, table_name, column_name, position):
    for name in (model, schema_name, table_name, column_name):
        if not isinstance(name, str) or not name or '\0' in name:
            raise ValueError('Stored column names must be nonempty strings without NUL.')
    if type(position) is not int or position < 0:
        raise ValueError('Column position must be a nonnegative integer.')


@dataclass(frozen=True)
class StoredColumn:
    model: str
    schema_name: str
    table_name: str
    column_name: str
    position: int

    def __post_init__(self):
        _validate_column_position(self.model, self.schema_name, self.table_name,
                                  self.column_name, self.position)


@dataclass(frozen=True)
class StoredOrder:
    model: str
    schema_name: str
    table_name: str
    column_name: str
    direction: Literal['ASC', 'DESC']
    position: int

    def __post_init__(self):
        _validate_column_position(self.model, self.schema_name, self.table_name,
                                  self.column_name, self.position)
        if self.direction not in ('ASC', 'DESC'):
            raise ValueError('Sort direction must be ASC or DESC.')


@dataclass(frozen=True)
class StoredRelationship:
    """A relationship to store; new records have no ID until saved."""

    id: int | None
    relationship: Relationship

    def __post_init__(self):
        if self.id is not None and (type(self.id) is not int or self.id <= 0):
            raise ValueError('Relationship ID must be a positive integer.')


@dataclass(frozen=True)
class NavigationDefault:
    """Identifier of a selected link within one browsing table and direction."""

    direction: Literal['parent', 'child']
    origin: Literal['database', 'user_defined']
    relationship_id: int | None
    constraint_name: str | None
    child_table: TableRef | None

    def __post_init__(self):
        if self.direction not in ('parent', 'child') or self.origin not in ('database', 'user_defined'):
            raise ValueError('Invalid navigation direction or relationship origin.')
        needs_child_table = self.origin == 'database' and self.direction == 'child'
        if (self.child_table is not None) != needs_child_table:
            raise ValueError('Only database Child selections require the declaring table.')
        if (self.relationship_id is None) == (self.constraint_name is None):
            raise ValueError('A selection requires exactly one relationship ID or constraint name.')
        if self.relationship_id is not None:
            minimum = 1 if self.origin == 'user_defined' else 0
            if type(self.relationship_id) is not int or self.relationship_id < minimum:
                raise ValueError('Invalid relationship ID.')
        elif self.origin == 'user_defined' or not self.constraint_name:
            raise ValueError('Only database selections can use a constraint name.')
