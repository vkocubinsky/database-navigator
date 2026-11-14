"""Per-window metadata cache; database reads remain on the calling worker."""

from threading import Lock


class MetadataCache:
    def __init__(self):
        self._lock = Lock()
        self._generation = 0
        self._aliases = {}
        self._links = {}
        self._columns = {}
        self._primary_keys = {}

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def reset(self):
        with self._lock:
            self._generation += 1
            self._aliases.clear()
            self._links.clear()
            self._columns.clear()
            self._primary_keys.clear()

    def relationship_diagram(self, browser, table, generation):
        """Collect key/endpoint columns for the direct diagram on one worker."""
        identity = self.resolve_table(browser, table, generation)
        links = self.relationships(browser, table, generation)
        dialect = browser.dialect

        def table_key(schema, name):
            return (dialect.identifier_key(schema or dialect.default_schema),
                    dialect.identifier_key(name))

        master_key = table_key(*identity)
        endpoints = {master_key: identity}
        participants = {master_key: []}
        for link in links:
            key = table_key(link.schema, link.table)
            endpoints.setdefault(key, (link.schema, link.table))
            participants.setdefault(key, []).extend(link.columns)
            participants[master_key].extend(link.selected_columns)
        columns = {}
        for key, (schema, name) in endpoints.items():
            with self._lock:
                primary = self._primary_keys.get(key) if generation == self._generation else None
            if primary is None:
                primary = tuple(browser.primary_key(name, schema))
                with self._lock:
                    if generation == self._generation:
                        self._primary_keys[key] = primary
            primary_names = {dialect.identifier_key(column) for column in primary}
            seen = set()
            selected = []
            for column in (*primary, *participants[key]):
                name_key = dialect.identifier_key(column)
                if name_key not in seen:
                    selected.append((column, name_key in primary_names))
                    seen.add(name_key)
            columns[key] = tuple(selected)
        return identity, links, columns

    def resolve_table(self, browser, table, generation):
        with self._lock:
            identity = self._aliases.get(table) if generation == self._generation else None
        if identity is None:
            identity = tuple(browser.resolve_table(table))
            with self._lock:
                if generation == self._generation:
                    self._aliases[table] = identity
        return identity

    def table_columns(self, browser, link, identity, generation):
        with self._lock:
            columns = self._columns.get(identity) if generation == self._generation else None
        if columns is None:
            columns = tuple(browser.table_columns(link))
            with self._lock:
                if generation == self._generation:
                    self._columns[identity] = columns
        return columns

    def relationships(self, browser, table, generation):
        with self._lock:
            identity = self._aliases.get(table) if generation == self._generation else None
            if identity in self._links:
                return list(self._links[identity])
        identity = self.resolve_table(browser, table, generation)
        with self._lock:
            if generation == self._generation and identity in self._links:
                self._aliases[table] = identity
                return list(self._links[identity])
        links = tuple(browser.relationships(table))
        with self._lock:
            if generation == self._generation:
                self._aliases[table] = identity
                self._links[identity] = links
        return list(links)
