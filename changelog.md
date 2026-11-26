# Changelog

## 0.8.0

- The UI and JSON metadata CLI now accept `--backend sqlite` or
  `--backend postgresql` with `--connection STRING`. For PostgreSQL, a launcher
  can use `--backend postgresql --connection "host=localhost dbname=example"`.
  Existing positional connection strings still work. Both commands reject
  unsupported URL schemes and URLs that conflict with the selected backend
  before opening a connection.

## 0.7.0

- **Open table** and **Open row** cancel a pending open if you change the row
  limit while the query is being prepared. Retry to use the new limit in both
  the SQL and the history entry.
- **Choose columns…** and **Set sort order…** wait for earlier saves before
  showing the current choices. If a saved column list or sort order contains
  a gap in its positions, the editor reports it and lets you reset or replace
  that setting.
- **Manage relationships** loads both tables' column lists together. Saving or
  deleting a relationship refreshes the current visit even if the dialog has
  closed.
- Python callers of `QueryGenerator` now pass column names in display order
  and `(column, direction)` pairs in sort order. The existing application
  database works with this release.
- Custom Python database dialects now implement `reference_from_sql(token)`.
  For catalog reads, diagrams, and metadata resets, use `Navigation`.
  Direct `DatabasePool` callers now supply a session factory and an optional
  worker count. To embed the relationship editor, use
  `RelationshipDialog(parent, controller.relationship_editor())`.

## 0.6.0

- New visits use the table’s saved direction, even when no relationship was
  saved. Without a saved direction, they select **Child** if the table has
  child relationships and **Parent** otherwise.
- Speed up SQLite relationship discovery on large databases by finding all
  tables that reference the current table in one catalog query.
- Keep generated queries tied to the correct table when a selected column
  name contains text such as `"sent from warehouse"`. This also fixes
  relationship navigation when resending the query or returning to it in history.
- Preserve the saved column order when applying **Choose columns…**. Newly
  selected columns follow the retained columns in table order.
- Fix a memory leak when resizing or refreshing the relationship diagram.
- The Python backend API now uses `query()` for related rows. Replace
  `backend.execute_related_query(query, limit)` with
  `backend.query(query.statement, limit, query.parameters)`.

## 0.5.0

- Clarify button labels with **Choose columns…**, **Set sort order…**,
  **Manage relationships**, and **View diagram**. Keep controls visible in
  narrow windows by wrapping them onto a second row.
- Remove the **Any** relationship filter. Choose **Parent** or **Child** to
  navigate in one direction, including for self-referencing tables. New visits
  default to **Parent** for tables without remembered relationships.
- Add **Choose table…** to find tables and views by name or schema patterns, then
  open generated SQL using saved column and sort selections and the row limit.
- Remember Parent and Child selections separately. Editing a custom mapping
  preserves the selection. Moving or deleting the relationship clears selections
  on tables that can no longer reach it.
- Fix a delay when reopening column and sort dialogs.
- Use parent and child tables and ordered keys in the Python metadata API and
  discovery JSON. PostgreSQL callers can request child relationships with
  `direction='child'`.

## 0.4.0

- Rename the Relationships button for consistency with other toolbar buttons.
- Refresh the Sakila screenshots in the README.

## 0.3.0

- License Database Navigator under GPL-3.0-or-later.

## 0.2.0

- Use model `default` when `--model` is omitted, enabling saved startup SQL,
  preferences, and custom relationships without extra launch arguments.

## 0.1.0

Initial release.
