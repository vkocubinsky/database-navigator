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

"""Searchable table picker; asynchronous loading and opening belong to the caller."""

import tkinter as tk
from tkinter import ttk

from .navigation import filter_tables


__all__ = ['TableDialog']


class TableDialog(tk.Toplevel):
    def __init__(self, parent, load, open_table):
        super().__init__(parent)
        self.load_callback = load
        self.open_callback = open_table
        self.tables = ()
        self.visible = ()
        self.revision = 0
        self.loading = False
        self.opening = False
        self.title('Choose table')
        self.transient(parent.winfo_toplevel())
        self.geometry('700x460')
        self.minsize(460, 320)
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.bind('<Escape>', lambda event: self.destroy())
        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Search tables').pack(anchor='w')
        self.search = tk.StringVar()
        self.entry = ttk.Entry(body, textvariable=self.search)
        self.entry.pack(fill='x', pady=(4, 6))
        hint = ttk.Label(body, text='Text finds part of a name. Patterns: film*, *_actor, public.*, pu*.cus*\n'
                         '* = any text   ? = one character   [abc] = one of these characters', justify='left')
        hint.pack(fill='x', pady=(0, 8))
        hint.bind('<Configure>', lambda event: hint.configure(wraplength=max(1, event.width)))
        frame = ttk.Frame(body)
        frame.pack(fill='both', expand=True)
        self.table_list = ttk.Treeview(frame, columns=('name', 'schema', 'kind'),
                                       show='headings', selectmode='browse')
        for name, label, width in (('name', 'Table', 310), ('schema', 'Schema', 180), ('kind', 'Type', 145)):
            self.table_list.heading(name, text=label)
            self.table_list.column(name, width=width, minwidth=width, stretch=name == 'name')
        vertical = ttk.Scrollbar(frame, orient='vertical', command=self.table_list.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=self.table_list.xview)
        self.table_list.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table_list.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.status = tk.StringVar()
        message = ttk.Label(body, textvariable=self.status, justify='left')
        message.pack(fill='x', pady=8)
        message.bind('<Configure>', lambda event: message.configure(wraplength=max(1, event.width)))
        actions = ttk.Frame(body)
        actions.pack(fill='x')
        self.retry_button = ttk.Button(actions, text='Retry', command=self.reload)
        self.open_button = ttk.Button(actions, text='Open', command=self.open_selected, state='disabled')
        self.open_button.pack(side='right')
        ttk.Button(actions, text='Cancel', command=self.destroy).pack(side='right', padx=8)
        self.search.trace_add('write', self.filter)
        self.table_list.bind('<<TreeviewSelect>>', self.update_open)
        self.table_list.bind('<Double-1>', self.double_click)
        self.table_list.bind('<Return>', self.open_selected)
        self.entry.bind('<Return>', self.open_selected)
        self.entry.bind('<Down>', self.focus_list)
        self.reload()
        self.grab_set()
        self.after_idle(self.entry.focus_set)

    def current_request(self):
        """Return a predicate that expires when this catalog is reloaded or closed."""
        revision = self.revision
        return lambda: revision == self.revision and bool(self.winfo_exists())

    def reload(self):
        self.revision += 1
        self.loading = True
        self.opening = False
        self.tables = self.visible = ()
        self.table_list.delete(*self.table_list.get_children())
        self.entry.configure(state='normal')
        self.table_list.state(['!disabled'])
        self.retry_button.pack_forget()
        self.status.set('Loading tables…')
        self.update_open()
        self.load_callback(self)

    def loaded(self, tables):
        self.tables = tables
        self.loading = False
        self.filter()

    def load_failed(self, error):
        self.loading = False
        self.status.set(f'Could not load tables: {error}')
        self.retry_button.pack(side='left')
        self.update_open()

    def selected(self):
        selected = self.table_list.selection()
        return self.visible[int(selected[0])].ref if selected else None

    def filter(self, *_):
        if self.loading or self.opening:
            return
        selected = self.selected()
        self.visible = filter_tables(self.tables, self.search.get())
        self.table_list.delete(*self.table_list.get_children())
        for index, table in enumerate(self.visible):
            self.table_list.insert('', 'end', iid=str(index),
                                   values=(table.ref.name, table.ref.schema, table.kind.replace('_', ' ').title()))
        if self.visible:
            index = next((i for i, table in enumerate(self.visible) if table.ref == selected), 0)
            self.table_list.selection_set(str(index))
            self.table_list.focus(str(index))
            self.table_list.see(str(index))
            self.status.set(f'{len(self.visible)} of {len(self.tables)} tables/views')
        else:
            self.status.set('No matching tables.' if self.tables else 'No user tables or views found.')
        self.update_open()

    def update_open(self, event=None):
        enabled = not self.loading and not self.opening and self.selected() is not None
        self.open_button.configure(state='normal' if enabled else 'disabled')

    def focus_list(self, event=None):
        if self.visible:
            self.table_list.focus_set()
        return 'break'

    def double_click(self, event):
        item = self.table_list.identify_row(event.y)
        if item and not self.opening:
            self.table_list.selection_set(item)
            self.open_selected()
        return 'break'

    def open_selected(self, event=None):
        ref = self.selected()
        if ref is not None and not self.loading and not self.opening:
            self.opening = True
            self.entry.configure(state='disabled')
            self.table_list.state(['disabled'])
            self.update_open()
            self.status.set(f'Preparing {ref.schema}.{ref.name}…')
            self.open_callback(self, ref)
        return 'break'

    def open_failed(self, error):
        self.opening = False
        self.entry.configure(state='normal')
        self.table_list.state(['!disabled'])
        self.status.set(str(error))
        self.update_open()
