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
import json
import os
import sqlite3

from .relationships import parse_relationships
from .contracts import DatabaseDialect


DEFAULT_DATABASE = "database-navigator.sqlite"


def _relationship_signature(key, dialect):
    identity = dialect.identifier_key
    return (identity(key.source_schema), identity(key.source_table),
            tuple(map(identity, key.source_columns)),
            identity(key.target_schema), identity(key.target_table),
            tuple(map(identity, key.target_columns)))


def validate_application_database(path):
    """Workers must share a persistent file, not separate temporary databases."""
    path = os.fsdecode(path)
    if not path or path == ':memory:' or '\0' in path:
        raise ValueError('The application database must be a persistent file path without NUL.')
    return path


class TableProperties:
    """Application storage for table/model properties and model relationships.

    Pass a local application database path, never a browsed database path.
    The default is database-navigator.sqlite in the current working directory.
    Each instance owns its connection and must be used on its creating thread.
    Use as a context manager or call close() explicitly.
    """

    def __init__(self, path=DEFAULT_DATABASE):
        self._connection = sqlite3.connect(validate_application_database(path))
        try:
            with self._connection:
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS table_properties (
                        model TEXT NOT NULL,
                        schema_name TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        property_name TEXT NOT NULL,
                        property_value TEXT NOT NULL,
                        PRIMARY KEY (model, schema_name, table_name, property_name)
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_properties (
                        model TEXT NOT NULL,
                        property_name TEXT NOT NULL,
                        property_value TEXT NOT NULL,
                        PRIMARY KEY (model, property_name)
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_relationships (
                        id INTEGER PRIMARY KEY,
                        model TEXT NOT NULL,
                        source_schema TEXT NOT NULL,
                        source_table TEXT NOT NULL,
                        target_schema TEXT NOT NULL,
                        target_table TEXT NOT NULL,
                        column_pairs TEXT NOT NULL,
                        UNIQUE (model, source_schema, source_table,
                                target_schema, target_table, column_pairs)
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

    def get(self, model, schema_name, table_name, property_name):
        """Return the stored text, or None when the property is absent."""
        key = self._key(model, schema_name, table_name, property_name)
        row = self._connection.execute("""
            SELECT property_value FROM table_properties
            WHERE model = ? AND schema_name = ? AND table_name = ? AND property_name = ?
        """, key).fetchone()
        return row[0] if row is not None else None

    def get_all(self, model, schema_name, table_name):
        """Return a name-to-text dictionary for one table, or an empty dictionary."""
        key = self._key(model, schema_name, table_name)
        return dict(self._connection.execute("""
            SELECT property_name, property_value FROM table_properties
            WHERE model = ? AND schema_name = ? AND table_name = ?
            ORDER BY property_name
        """, key))

    def set(self, model, schema_name, table_name, property_name, property_value):
        """Insert or replace one property, committing it immediately.

        Values are opaque text: this layer neither parses nor executes SQL.
        Property-specific validation belongs to the consuming code.
        """
        self.set_many(model, schema_name, table_name, {property_name: property_value})

    def set_many(self, model, schema_name, table_name, properties):
        """Save related properties atomically, validating all values before writing."""
        key = self._key(model, schema_name, table_name)
        rows = []
        for name, value in properties.items():
            self._key(name)
            if not isinstance(value, str):
                raise ValueError("Property values must be strings; delete a property to reset it.")
            rows.append((*key, name, value))
        with self._connection:
            self._connection.executemany("""
                INSERT INTO table_properties
                    (model, schema_name, table_name, property_name, property_value)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (model, schema_name, table_name, property_name)
                DO UPDATE SET property_value = excluded.property_value
            """, rows)

    def delete(self, model, schema_name, table_name, property_name):
        """Reset a property by deleting its record; absent properties are harmless."""
        key = self._key(model, schema_name, table_name, property_name)
        with self._connection:
            self._connection.execute("""
                DELETE FROM table_properties
                WHERE model = ? AND schema_name = ? AND table_name = ? AND property_name = ?
            """, key)

    def get_model(self, model, property_name):
        key = self._key(model, property_name)
        row = self._connection.execute("""
            SELECT property_value FROM model_properties
            WHERE model = ? AND property_name = ?
        """, key).fetchone()
        return row[0] if row is not None else None

    def set_model(self, model, property_name, property_value):
        key = self._key(model, property_name)
        if not isinstance(property_value, str):
            raise ValueError('Model property values must be strings.')
        with self._connection:
            self._connection.execute("""
                INSERT INTO model_properties (model, property_name, property_value)
                VALUES (?, ?, ?)
                ON CONFLICT (model, property_name)
                DO UPDATE SET property_value = excluded.property_value
            """, (*key, property_value))

    def delete_model(self, model, property_name):
        key = self._key(model, property_name)
        with self._connection:
            self._connection.execute("""
                DELETE FROM model_properties WHERE model = ? AND property_name = ?
            """, key)

    def get_relationships(self, model, *, dialect: DatabaseDialect):
        """Load and validate a model's declarations in insertion order."""
        return tuple(key for _, key in self.get_relationship_records(model, dialect=dialect))

    def get_relationship_records(self, model, *, dialect: DatabaseDialect):
        """Return stable local IDs and validated declarations for the editor."""
        self._key(model)
        items = []
        with closing(self._connection.execute("""
            SELECT id, source_schema, source_table, target_schema, target_table, column_pairs
            FROM model_relationships WHERE model = ? ORDER BY id
        """, (model,))) as cursor:
            rows = cursor.fetchall()
        # Validation errors may retain their traceback; never retain a read lock with it.
        for identifier, source_schema, source_table, target_schema, target_table, value in rows:
            try:
                pairs = json.loads(value)
                if (not isinstance(pairs, list) or not pairs
                        or any(not isinstance(pair, list) or len(pair) != 2 for pair in pairs)):
                    raise ValueError('Expected a nonempty array of source/target column pairs.')
                item = {
                    'source': {'schema': source_schema, 'table': source_table,
                               'columns': [pair[0] for pair in pairs]},
                    'target': {'schema': target_schema, 'table': target_table,
                               'columns': [pair[1] for pair in pairs]},
                }
                key, = parse_relationships({'relationships': [item]}, dialect=dialect)
                items.append((identifier, key))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid stored relationship {identifier} for model {model!r}: {exc}') from None
        return tuple(items)

    def save_relationship(self, model, key, identifier=None, *, dialect: DatabaseDialect):
        """Insert or update one declaration, rejecting duplicates within the model."""
        self._key(model)
        document = {'relationships': [{
            'source': {'schema': key.source_schema, 'table': key.source_table,
                       'columns': list(key.source_columns)},
            'target': {'schema': key.target_schema, 'table': key.target_table,
                       'columns': list(key.target_columns)}}]}
        key, = parse_relationships(document, dialect=dialect)

        values = (key.source_schema, key.source_table, key.target_schema, key.target_table,
                  json.dumps(list(zip(key.source_columns, key.target_columns)), separators=(',', ':')))
        with self._connection:
            # Serialize duplicate detection and the mutation across application windows.
            self._connection.execute('BEGIN IMMEDIATE')
            records = self.get_relationship_records(model, dialect=dialect)
            if identifier is not None and not any(row_id == identifier for row_id, _ in records):
                raise ValueError('This relationship no longer exists in the current model. Reload the list.')
            signature = _relationship_signature(key, dialect)
            if any(row_id != identifier and _relationship_signature(existing, dialect) == signature
                   for row_id, existing in records):
                raise ValueError('That custom relationship already exists in this model.')
            if identifier is None:
                cursor = self._connection.execute('''
                    INSERT INTO model_relationships
                        (model, source_schema, source_table, target_schema, target_table, column_pairs)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (model, *values))
                return cursor.lastrowid
            self._connection.execute('''
                UPDATE model_relationships SET source_schema = ?, source_table = ?,
                    target_schema = ?, target_table = ?, column_pairs = ?
                WHERE model = ? AND id = ?
            ''', (*values, model, identifier))
            return identifier

    def delete_relationship(self, model, identifier):
        self._key(model)
        with self._connection:
            self._connection.execute('DELETE FROM model_relationships WHERE model = ? AND id = ?',
                                     (model, identifier))

    def add_relationships(self, model, keys, *, dialect: DatabaseDialect):
        """Atomically add declarations, skipping identical mappings; return added count."""
        self._key(model)
        document = {'relationships': [
            {'source': {'schema': key.source_schema, 'table': key.source_table,
                        'columns': list(key.source_columns)},
             'target': {'schema': key.target_schema, 'table': key.target_table,
                        'columns': list(key.target_columns)}} for key in keys]}
        keys = parse_relationships(document, dialect=dialect)
        with self._connection:
            # Match the editor's identity rules, including SQLite ASCII folding.
            # Lock before reading so concurrent import/edit sessions cannot race.
            self._connection.execute('BEGIN IMMEDIATE')
            seen = {_relationship_signature(key, dialect)
                    for _, key in self.get_relationship_records(model, dialect=dialect)}
            rows = []
            for key in keys:
                signature = _relationship_signature(key, dialect)
                if signature in seen:
                    continue
                seen.add(signature)
                rows.append((model, key.source_schema, key.source_table, key.target_schema, key.target_table,
                             json.dumps(list(zip(key.source_columns, key.target_columns)), separators=(',', ':'))))
            before = self._connection.total_changes
            self._connection.executemany("""
                INSERT INTO model_relationships
                    (model, source_schema, source_table, target_schema, target_table, column_pairs)
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)
            return self._connection.total_changes - before
