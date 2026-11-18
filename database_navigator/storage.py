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

"""Local preferences and relationships, separate from read-only browsed databases."""

from contextlib import closing
from dataclasses import replace
from itertools import groupby
import os
import sqlite3

from .metadata_models import Relationship, TableRef
from .contracts import DatabaseDialect
from .storage_models import NavigationDefault, StoredColumn, StoredOrder, StoredRelationship


__all__ = [
    "ApplicationStore",
    "DEFAULT_DATABASE",
    "validate_relationship",
    "validate_application_database",
]


DEFAULT_DATABASE = "database-navigator.sqlite"


def validate_relationship(key: Relationship, *, dialect: DatabaseDialect) -> None:
    """Check dialect rules for a typed mapping; intrinsic validation lives in metadata."""
    for ref, columns in ((key.child_table, key.child_key), (key.parent_table, key.parent_key)):
        dialect.validate_schema(ref.schema)
        for name in (ref.schema, ref.name, *columns):
            dialect.validate_identifier(name)
        if len({dialect.identifier_key(name) for name in columns}) != len(columns):
            raise ValueError('Relationship has duplicate columns.')


def _relationship_signature(key, dialect):
    identity = dialect.identifier_key
    return (identity(key.child_table.schema), identity(key.child_table.name),
            tuple(map(identity, key.child_key)),
            identity(key.parent_table.schema), identity(key.parent_table.name),
            tuple(map(identity, key.parent_key)))


def validate_application_database(path):
    """Workers must share a persistent file, not separate temporary databases."""
    path = os.fsdecode(path)
    if not path or path == ':memory:' or '\0' in path:
        raise ValueError('The application database must be a persistent file path without NUL.')
    return path


class ApplicationStore:
    """Application storage for table/model properties and model relationships.

    Pass a local application database path, never a browsed database path.
    The default is database-navigator.sqlite in the current working directory.
    Each instance owns its connection and must be used on its creating thread.
    Use as a context manager or call close() explicitly.
    """

    def __init__(self, path=DEFAULT_DATABASE):
        self._connection = sqlite3.connect(validate_application_database(path))
        try:
            self._connection.execute('PRAGMA foreign_keys = ON')
            with self._connection:
                # Create the local storage schema atomically across workers.
                self._connection.execute('BEGIN IMMEDIATE')
                if self._connection.execute("""
                    SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name IN (
                        'table_properties', 'model_properties', 'table_columns', 'model_relationships',
                        'model_relationship_columns', 'table_navigation_defaults')
                """).fetchone():
                    raise ValueError('This application database uses the old relationship schema. '
                                     'Choose a fresh application database.')
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS table_property (
                        model TEXT NOT NULL,
                        schema_name TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        property_name TEXT NOT NULL,
                        property_value TEXT NOT NULL,
                        PRIMARY KEY (model, schema_name, table_name, property_name)
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_property (
                        model TEXT NOT NULL,
                        property_name TEXT NOT NULL,
                        property_value TEXT NOT NULL,
                        PRIMARY KEY (model, property_name)
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_relationship (
                        model TEXT NOT NULL,
                        relationship_id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (relationship_id > 0),
                        parent_schema TEXT NOT NULL,
                        parent_table TEXT NOT NULL,
                        child_schema TEXT NOT NULL,
                        child_table TEXT NOT NULL
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_relationship_column (
                        relationship_id INTEGER NOT NULL
                            REFERENCES model_relationship(relationship_id) ON DELETE CASCADE,
                        position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 0),
                        parent_column TEXT NOT NULL,
                        child_column TEXT NOT NULL,
                        PRIMARY KEY (relationship_id, position),
                        UNIQUE (relationship_id, child_column),
                        UNIQUE (relationship_id, parent_column)
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS table_navigation_default (
                        model TEXT NOT NULL,
                        schema_name TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        direction TEXT NOT NULL CHECK (direction IN ('parent', 'child')),
                        origin TEXT NOT NULL CHECK (origin IN ('database', 'user_defined')),
                        relationship_id INTEGER,
                        constraint_name TEXT,
                        child_schema TEXT,
                        child_table TEXT,
                        PRIMARY KEY (model, schema_name, table_name, direction),
                        CHECK ((relationship_id IS NULL) != (constraint_name IS NULL)),
                        CHECK (relationship_id IS NULL OR
                               (typeof(relationship_id) = 'integer' AND relationship_id >= 0)),
                        CHECK (constraint_name IS NULL OR length(constraint_name) > 0),
                        CHECK (origin = 'database' OR (relationship_id IS NOT NULL AND relationship_id > 0)),
                        CHECK ((child_schema IS NOT NULL) = (origin = 'database' AND direction = 'child')),
                        CHECK ((child_table IS NOT NULL) = (child_schema IS NOT NULL))
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS table_column (
                        model TEXT NOT NULL,
                        schema_name TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        column_name TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 0),
                        PRIMARY KEY (model, schema_name, table_name, column_name),
                        UNIQUE (model, schema_name, table_name, position)
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS table_order (
                        model TEXT NOT NULL,
                        schema_name TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        column_name TEXT NOT NULL,
                        direction TEXT NOT NULL CHECK (direction IN ('ASC', 'DESC')),
                        position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 0),
                        PRIMARY KEY (model, schema_name, table_name, column_name),
                        UNIQUE (model, schema_name, table_name, position)
                    )
                """)
        except Exception:
            self._connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._connection.close()

    @staticmethod
    def _key(*parts):
        if any(not isinstance(part, str) or not part or '\0' in part for part in parts):
            raise ValueError("Property keys must be nonempty strings without NUL.")
        return parts

    @staticmethod
    def _text_property(name):
        if name == 'columns':
            raise ValueError('Use columns() or replace_columns() for selected columns.')
        if name == 'order_by':
            raise ValueError('Use order() or replace_order() for sort keys.')
        if name in ('default_parent_link', 'default_child_link'):
            raise ValueError('Use save_navigation_default() for remembered relationships.')

    @staticmethod
    def _selection_rows(key, rows, row_type):
        rows = tuple(rows)
        for position, row in enumerate(rows):
            if not isinstance(row, row_type):
                raise ValueError(f'Supply {row_type.__name__} records.')
            if (row.model, row.schema_name, row.table_name) != key:
                raise ValueError('Every record must belong to the selected model and table.')
            if row.position != position:
                raise ValueError('Positions must be contiguous starting at zero, in supplied order.')
        if len({row.column_name for row in rows}) != len(rows):
            raise ValueError('Choose each column only once.')
        return rows

    def columns(self, model: str, table: TableRef) -> tuple[StoredColumn, ...]:
        """Read selected columns in projection order; no rows means All columns."""
        key = self._key(model, table.schema, table.name)
        with closing(self._connection.execute('''
            SELECT model, schema_name, table_name, column_name, position
            FROM table_column WHERE model = ? AND schema_name = ? AND table_name = ?
            ORDER BY position
        ''', key)) as cursor:
            rows = cursor.fetchall()
        return tuple(StoredColumn(*row) for row in rows)

    def replace_columns(self, model: str, table: TableRef, rows: tuple[StoredColumn, ...]) -> None:
        """Atomically replace a selection; an empty tuple resets to All columns."""
        key = self._key(model, table.schema, table.name)
        rows = self._selection_rows(key, rows, StoredColumn)
        with self._connection:
            self._connection.execute('''
                DELETE FROM table_column WHERE model = ? AND schema_name = ? AND table_name = ?
            ''', key)
            self._connection.executemany('''
                INSERT INTO table_column (model, schema_name, table_name, column_name, position)
                VALUES (?, ?, ?, ?, ?)
            ''', ((*key, row.column_name, row.position) for row in rows))

    def order(self, model: str, table: TableRef) -> tuple[StoredOrder, ...]:
        """Read sort keys in priority order; no rows means no explicit ordering."""
        key = self._key(model, table.schema, table.name)
        with closing(self._connection.execute('''
            SELECT model, schema_name, table_name, column_name, direction, position
            FROM table_order WHERE model = ? AND schema_name = ? AND table_name = ?
            ORDER BY position
        ''', key)) as cursor:
            rows = cursor.fetchall()
        return tuple(StoredOrder(*row) for row in rows)

    def replace_order(self, model: str, table: TableRef, rows: tuple[StoredOrder, ...]) -> None:
        """Atomically replace sort keys; an empty tuple resets to No order."""
        key = self._key(model, table.schema, table.name)
        rows = self._selection_rows(key, rows, StoredOrder)
        with self._connection:
            self._connection.execute('''
                DELETE FROM table_order WHERE model = ? AND schema_name = ? AND table_name = ?
            ''', key)
            self._connection.executemany('''
                INSERT INTO table_order (model, schema_name, table_name, column_name, direction, position)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ((*key, row.column_name, row.direction, row.position) for row in rows))

    def get(self, model, schema_name, table_name, property_name):
        """Return the stored text, or None when the property is absent."""
        self._text_property(property_name)
        key = self._key(model, schema_name, table_name, property_name)
        row = self._connection.execute("""
            SELECT property_value FROM table_property
            WHERE model = ? AND schema_name = ? AND table_name = ? AND property_name = ?
        """, key).fetchone()
        return row[0] if row is not None else None

    def get_all(self, model, schema_name, table_name):
        """Return text preferences only; columns and sort keys have their own APIs."""
        key = self._key(model, schema_name, table_name)
        return dict(self._connection.execute("""
            SELECT property_name, property_value FROM table_property
            WHERE model = ? AND schema_name = ? AND table_name = ?
                AND property_name NOT IN ('columns', 'order_by', 'default_parent_link',
                                         'default_child_link')
            ORDER BY property_name
        """, key))

    def set(self, model, schema_name, table_name, property_name, property_value):
        """Insert or replace one property, committing it immediately.

        Values are opaque text: this layer neither parses nor executes SQL.
        Property-specific validation belongs to the consuming code.
        """
        self._text_property(property_name)
        key = self._key(model, schema_name, table_name, property_name)
        if not isinstance(property_value, str):
            raise ValueError("Property values must be strings; delete a property to reset it.")
        with self._connection:
            self._connection.execute("""
                INSERT INTO table_property
                    (model, schema_name, table_name, property_name, property_value)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (model, schema_name, table_name, property_name)
                DO UPDATE SET property_value = excluded.property_value
            """, (*key, property_value))

    def delete(self, model, schema_name, table_name, property_name):
        """Reset a property by deleting its record; absent properties are harmless."""
        self._text_property(property_name)
        key = self._key(model, schema_name, table_name, property_name)
        with self._connection:
            self._connection.execute("""
                DELETE FROM table_property
                WHERE model = ? AND schema_name = ? AND table_name = ? AND property_name = ?
            """, key)

    def get_model(self, model, property_name):
        key = self._key(model, property_name)
        row = self._connection.execute("""
            SELECT property_value FROM model_property
            WHERE model = ? AND property_name = ?
        """, key).fetchone()
        return row[0] if row is not None else None

    def set_model(self, model, property_name, property_value):
        key = self._key(model, property_name)
        if not isinstance(property_value, str):
            raise ValueError('Model property values must be strings.')
        with self._connection:
            self._connection.execute("""
                INSERT INTO model_property (model, property_name, property_value)
                VALUES (?, ?, ?)
                ON CONFLICT (model, property_name)
                DO UPDATE SET property_value = excluded.property_value
            """, (*key, property_value))

    def delete_model(self, model, property_name):
        key = self._key(model, property_name)
        with self._connection:
            self._connection.execute("""
                DELETE FROM model_property WHERE model = ? AND property_name = ?
            """, key)

    def relationships(self, model, *, dialect: DatabaseDialect) -> tuple[StoredRelationship, ...]:
        """Read custom declarations only, for discovery and the relationship editor."""
        self._key(model)
        with closing(self._connection.execute("""
            SELECT relationship_id, parent_schema, parent_table, child_schema, child_table,
                   parent_column, child_column, position
            FROM model_relationship
            LEFT JOIN model_relationship_column USING (relationship_id)
            WHERE model = ?
            ORDER BY relationship_id, position
        """, (model,))) as cursor:
            rows = cursor.fetchall()
        # Validation errors may retain their traceback; never retain a read lock with it.
        items = []
        for identifier, group in groupby(rows, key=lambda row: row[0]):
            pairs = list(group)
            try:
                parent_schema, parent_table, child_schema, child_table = pairs[0][1:5]
                if [row[7] for row in pairs] != list(range(len(pairs))):
                    raise ValueError('Each relationship needs column pairs with contiguous positions starting at zero.')
                key = Relationship(
                    parent_table=TableRef(parent_schema, parent_table),
                    child_table=TableRef(child_schema, child_table),
                    parent_key=tuple(row[5] for row in pairs),
                    child_key=tuple(row[6] for row in pairs),
                    origin='user_defined',
                )
                validate_relationship(key, dialect=dialect)
                items.append(StoredRelationship(identifier, key))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid stored relationship {identifier} for model {model!r}: {exc}') from None
        return tuple(items)

    @staticmethod
    def _relationship_id(identifier):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError('Relationship ID must be a positive integer.')

    def default_relationships(self, model, owner: TableRef) -> tuple[NavigationDefault, ...]:
        rows = self._connection.execute("""
            SELECT direction, origin, relationship_id, constraint_name, child_schema, child_table
            FROM table_navigation_default
            WHERE model = ? AND schema_name = ? AND table_name = ?
            ORDER BY direction
        """, self._key(model, owner.schema, owner.name)).fetchall()
        return tuple(NavigationDefault(direction, origin, identifier, name,
                                       TableRef(schema, table) if table is not None else None)
                     for direction, origin, identifier, name, schema, table in rows)

    def save_navigation_default(self, model, owner: TableRef, selection: NavigationDefault, *, dialect: DatabaseDialect):
        key = self._key(model, owner.schema, owner.name)
        child = selection.child_table
        with self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            if selection.origin == 'user_defined':
                endpoint = 'child' if selection.direction == 'parent' else 'parent'
                row = self._connection.execute(f"""
                    SELECT {endpoint}_schema, {endpoint}_table FROM model_relationship
                    WHERE model = ? AND relationship_id = ?
                """, (model, selection.relationship_id)).fetchone()
                identity = dialect.identifier_key
                if row is None or (identity(row[0]), identity(row[1])) != (identity(owner.schema), identity(owner.name)):
                    raise ValueError('The custom relationship no longer belongs to this table.')
            self._connection.execute("""
                INSERT INTO table_navigation_default
                    (model, schema_name, table_name, direction, origin, relationship_id,
                     constraint_name, child_schema, child_table)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (model, schema_name, table_name, direction) DO UPDATE SET
                    origin = excluded.origin, relationship_id = excluded.relationship_id,
                    constraint_name = excluded.constraint_name, child_schema = excluded.child_schema,
                    child_table = excluded.child_table
            """, (*key, selection.direction, selection.origin, selection.relationship_id,
                  selection.constraint_name, child.schema if child else None, child.name if child else None))
            self._connection.execute("""
                INSERT INTO table_property (model, schema_name, table_name, property_name, property_value)
                VALUES (?, ?, ?, 'default_direction', ?)
                ON CONFLICT (model, schema_name, table_name, property_name)
                DO UPDATE SET property_value = excluded.property_value
            """, (*key, selection.direction))

    def save_relationship(self, model: str, record: StoredRelationship,
                          *, dialect: DatabaseDialect) -> StoredRelationship:
        """Save a custom declaration and return its assigned ID."""
        self._key(model)
        validate_relationship(record.relationship, dialect=dialect)
        with self._connection:
            # Serialize identity checks and the complete mutation across workers.
            self._connection.execute('BEGIN IMMEDIATE')
            record = replace(record, relationship=replace(record.relationship, origin='user_defined', name=None, relationship_id=None))
            records = self.relationships(model, dialect=dialect)
            previous = next((item.relationship for item in records if item.id == record.id), None)
            if record.id is not None and previous is None:
                raise ValueError('This relationship no longer exists in the current model. Reload the list.')
            signature = _relationship_signature(record.relationship, dialect)
            if any(existing.id != record.id and _relationship_signature(existing.relationship, dialect) == signature
                   for existing in records):
                raise ValueError('That custom relationship already exists in this model.')
            key = record.relationship
            endpoints = (key.parent_table.schema, key.parent_table.name, key.child_table.schema, key.child_table.name)
            if record.id is None:
                cursor = self._connection.execute("""
                    INSERT INTO model_relationship (parent_schema, parent_table, child_schema, child_table, model)
                    VALUES (?, ?, ?, ?, ?)
                """, (*endpoints, model))
                record = replace(record, id=cursor.lastrowid)
            else:
                self._connection.execute("""
                    UPDATE model_relationship SET parent_schema = ?, parent_table = ?, child_schema = ?, child_table = ?
                    WHERE model = ? AND relationship_id = ?
                """, (*endpoints, model, record.id))
                identity = dialect.identifier_key
                for direction, old_owner, new_owner in (
                    ('parent', previous.child_table, key.child_table),
                    ('child', previous.parent_table, key.parent_table),
                ):
                    if (identity(old_owner.schema), identity(old_owner.name)) != (
                            identity(new_owner.schema), identity(new_owner.name)):
                        self._connection.execute("""
                            DELETE FROM table_navigation_default
                            WHERE model = ? AND origin = 'user_defined' AND relationship_id = ? AND direction = ?
                        """, (model, record.id, direction))
                self._connection.execute('DELETE FROM model_relationship_column WHERE relationship_id = ?', (record.id,))
            pairs = zip(key.parent_key, key.child_key, strict=True)
            self._connection.executemany("""
                INSERT INTO model_relationship_column (relationship_id, position, parent_column, child_column)
                VALUES (?, ?, ?, ?)
            """, ((record.id, position, parent, child) for position, (parent, child) in enumerate(pairs)))
            return record

    def delete_relationship(self, model, identifier):
        self._key(model)
        self._relationship_id(identifier)
        with self._connection:
            self._connection.execute("DELETE FROM model_relationship WHERE model = ? AND relationship_id = ?",
                                     (model, identifier))
            self._connection.execute("""
                DELETE FROM table_navigation_default
                WHERE model = ? AND origin = 'user_defined' AND relationship_id = ?
            """, (model, identifier))
