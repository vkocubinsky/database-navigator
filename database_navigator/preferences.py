"""Worker-owned table preferences and validated generated-query options."""

import json
from dataclasses import replace

from .storage import TableProperties, validate_application_database
from .relationships import merge_relationships, parse_relationships
from .metadata import MetadataCache
from .contracts import DatabaseBackend
from .models import Relationship, link_identity, parse_columns, stored_column_name


def validate_configuration(browser: DatabaseBackend, model, path):
    if model is None:
        return
    if not isinstance(model, str) or not model.strip() or '\0' in model:
        raise ValueError('Model must be a nonempty name without NUL.')
    path = validate_application_database(path)
    browser.validate_application_database(path)


class PreferenceSession:
    """One worker's browser and lazily opened application storage connection."""

    def __init__(self, browser: DatabaseBackend, model, path, *, metadata=None):
        if model is None:
            raise ValueError('Preference sessions require a model.')
        validate_configuration(browser, model, path)
        self.browser = browser
        self.dialect = browser.dialect
        self.model = model
        self.path = path
        self._store = None
        self.metadata = metadata if metadata is not None else MetadataCache()

    def resolve_table(self, table):
        return self.browser.resolve_table(table)

    def query(self, sql, limit, parameters=()):
        return self.browser.query(sql, limit, parameters)

    def primary_key(self, table, schema=None):
        return self.browser.primary_key(table, schema)

    def execute_related_query(self, query, limit):
        return self.browser.execute_related_query(query, limit)

    @property
    def store(self):
        if self._store is None:
            self._store = TableProperties(self.path)
        return self._store

    def close(self):
        try:
            self.browser.close()
        finally:
            if self._store is not None:
                self._store.close()

    def _key(self, link, generation=None):
        if generation is None:
            generation = self.metadata.generation
        table = self.dialect.table_reference(link.table, link.schema)
        return (self.model, *self.metadata.resolve_table(self.browser, table, generation))

    def _columns(self, link, key, generation):
        return self.metadata.table_columns(self.browser, link, key[1:], generation)

    def startup_sql(self):
        """Read local preferences only; never open the browsed database."""
        return self.store.get_model(self.model, 'start_sql') or ''

    def relationships(self, table):
        """Merge model declarations on the worker; metadata reset reloads storage."""
        keys = self.store.get_relationships(self.model, dialect=self.dialect)
        result = self.browser.relationships(table)
        if keys:
            schema, name = self.dialect.split_reference(table)
            if schema is None:
                schema, name = self.browser.resolve_table(table)
            merge_relationships(result, keys, schema, name, dialect=self.dialect)
        return result

    def custom_relationships(self):
        return self.store.get_relationship_records(
            self.model, dialect=self.dialect)

    def relationship_columns(self, schema, table):
        """Fresh, read-only endpoint discovery, independent of cached navigation metadata."""
        if not isinstance(table, str) or not table or '\0' in table:
            raise ValueError('Enter a table name without NUL.')
        name = self.dialect.table_reference(table, schema)
        schema, table = self.browser.resolve_table(name)
        self.dialect.validate_schema(schema)
        self.dialect.validate_identifier(table)
        link = Relationship(table, (), (), '', schema)
        return schema, table, tuple(self.browser.table_columns(link))

    def save_custom_relationship(self, identifier, document):
        keys = parse_relationships(document, dialect=self.dialect)
        if len(keys) != 1:
            raise ValueError('Save one relationship at a time.')
        key, = keys
        endpoints = []
        for schema, table, columns in ((key.source_schema, key.source_table, key.source_columns),
                                       (key.target_schema, key.target_table, key.target_columns)):
            schema, table, available = self.relationship_columns(schema, table)
            normalized = {self.dialect.identifier_key(name): name for name in available}
            chosen = []
            for column in columns:
                name = normalized.get(self.dialect.identifier_key(column))
                if name is None:
                    raise ValueError(f'Column {column!r} no longer exists in {schema}.{table}. Reload columns.')
                chosen.append(name)
            endpoints.append((schema, table, tuple(chosen)))
        source, target = endpoints
        key = replace(key, source_schema=source[0], source_table=source[1], source_columns=source[2],
                      target_schema=target[0], target_table=target[1], target_columns=target[2])
        return self.store.save_relationship(self.model, key, identifier, dialect=self.dialect)

    def delete_custom_relationship(self, identifier):
        self.store.delete_relationship(self.model, identifier)

    def save_startup_sql(self, text):
        if text.strip():
            self.store.set_model(self.model, 'start_sql', text)
        else:
            self.store.delete_model(self.model, 'start_sql')

    def table_preferences(self, link):
        # Return raw stored text so a dialog can reset even a stale/invalid preference.
        generation = self.metadata.generation
        key = self._key(link, generation)
        return self._columns(link, key, generation), self.store.get_all(*key)

    def navigation_defaults(self, table):
        key = (self.model, *self.browser.resolve_table(table))
        return key, self.store.get_all(*key)

    def save_navigation_default(self, table, direction, link=None):
        if direction not in ('child', 'parent', 'any'):
            raise ValueError('Unknown navigation direction.')
        key = (self.model, *self.browser.resolve_table(table))
        if link is None:
            self.store.set(*key, 'default_direction', direction)
        else:
            identity = link_identity(link)
            if direction != 'any' and direction != identity['direction']:
                raise ValueError('The relationship does not match the selected direction.')
            self.store.set_many(*key, {
                f'default_{direction}_link': json.dumps(identity),
                'default_direction': direction,
            })

    def _validate(self, columns, selected, order):
        for names in (selected, tuple(name for name, _ in order)):
            if len(names) != len(set(names)):
                raise ValueError('Choose each column only once.')
            for name in names:
                if name not in columns:
                    raise ValueError(f'Column {name!r} no longer exists. Update Columns or Order.')
                self.dialect.validate_identifier(name)
        if any(direction not in ('ASC', 'DESC') for _, direction in order):
            raise ValueError('Sort direction must be ASC or DESC.')

    def save_preference(self, link, property_name, choices):
        if property_name not in ('columns', 'order_by'):
            raise ValueError('Unsupported table preference.')
        generation = self.metadata.generation
        key = self._key(link, generation)
        if choices is None:
            self.store.delete(*key, property_name)
            return
        if not choices:
            raise ValueError('Choose at least one column.')
        selected, order = (tuple(choices), ()) if property_name == 'columns' else ((), tuple(choices))
        self._validate(self._columns(link, key, generation), selected, order)
        value = (', '.join(stored_column_name(name) for name in selected) if selected else
                 ', '.join(f'{stored_column_name(name)} {direction}' for name, direction in order))
        self.store.set(*key, property_name, value)

    def _options(self, link):
        generation = self.metadata.generation
        key = self._key(link, generation)
        saved = self.store.get_all(*key)
        selected = parse_columns(saved.get('columns'))
        order = parse_columns(saved.get('order_by'), ordering=True)
        if selected or order:
            self._validate(self._columns(link, key, generation), selected, order)
        return selected or None, order

    def navigation_query(self, link, columns, row, limit):
        return self.browser.navigation_query(link, columns, row, limit, *self._options(link))

    def row_navigation_query(self, link, columns, row, limit):
        return self.browser.row_navigation_query(link, columns, row, limit, *self._options(link))
