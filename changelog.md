# Changelog

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
