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

"""A selectable query result with original values and a private Treeview."""

from tkinter import ttk


__all__ = ["DataTable"]


class DataTable(ttk.Frame):
    def __init__(self, parent, height=5):
        super().__init__(parent)
        self.columns, self.rows = (), []
        self._tree = ttk.Treeview(self, show="headings", selectmode="browse", height=height)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def show(self, columns, rows):
        self.columns, self.rows = tuple(columns), rows
        self._tree.delete(*self._tree.get_children())
        ids = [str(i) for i in range(len(columns))]
        self._tree.configure(columns=ids)
        for identifier, name in zip(ids, columns):
            self._tree.heading(identifier, text=name)
            self._tree.column(identifier, width=130, minwidth=70, stretch=False)
        for index, row in enumerate(rows):
            self._tree.insert("", "end", iid=str(index), values=[
                "NULL" if value is None else str(value) for value in row
            ])

    def clear(self):
        self.show((), [])

    def selected_index(self):
        selected = self._tree.selection()
        return int(selected[0]) if selected else None

    def select(self, index):
        if index is None:
            self._tree.selection_remove(*self._tree.selection())
        else:
            self._tree.selection_set(str(index))
            self._tree.focus(str(index))
            self._tree.see(str(index))

    def _selected_values(self):
        index = self.selected_index()
        return self.rows[index] if index is not None else None

    def selected_row(self):
        """Return original values by name, rejecting ambiguous result columns."""
        values = self._selected_values()
        if values is None:
            return None
        if len(set(self.columns)) != len(self.columns):
            raise ValueError('Navigation requires unambiguous column names. Use unique column aliases for duplicate names.')
        return dict(zip(self.columns, values, strict=True))

    def bind_event(self, sequence, callback):
        def deliver(event):
            event.widget = self
            return callback(event)
        self._tree.bind(sequence, deliver)

    def event_generate(self, sequence, **kwargs):
        self._tree.event_generate(sequence, **kwargs)

    def focus_set(self):
        self._tree.focus_set()

    def can_focus(self):
        return self.winfo_ismapped() and not self._tree.instate(['disabled']) and bool(self.rows)

    def focus_selection(self):
        index = self.selected_index()
        if self.rows:
            self.select(0 if index is None else index)
        self.focus_set()
