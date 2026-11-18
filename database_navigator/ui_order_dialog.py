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

"""Sort-key editor with priority and direction; persistence is delegated to the caller."""

import tkinter as tk
from tkinter import ttk


__all__ = ["OrderDialog"]


class OrderDialog(tk.Toplevel):
    def __init__(self, parent, link, columns, selected, apply, *, error=''):
        super().__init__(parent)
        self.apply_callback = apply
        self.columns = columns
        self.saving = False
        self.title(f'Order — {link.display_table}')
        self.transient(parent.winfo_toplevel())
        self.geometry('560x480')
        self.minsize(460, 350)
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.bind('<Escape>', lambda event: self.cancel())
        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=link.display_table, font=('TkDefaultFont', 12, 'bold')).pack(anchor='w')
        mode_bar = ttk.Frame(body)
        mode_bar.pack(fill='x', pady=10)
        self.mode = tk.StringVar(value='choose' if selected is not None else 'default')
        for label, value in (('No order', 'default'), ('Choose', 'choose')):
            ttk.Radiobutton(mode_bar, text=label, value=value, variable=self.mode,
                            command=self.update_mode).pack(side='left', padx=(0, 12))
        self.error = tk.StringVar(value=error)
        self.controls = ttk.Frame(body)
        self.controls.pack(fill='both', expand=True)
        self.build_order(selected or ())
        ttk.Label(body, textvariable=self.error, wraplength=510).pack(fill='x', pady=8)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x')
        self.apply_button = ttk.Button(buttons, text='Apply', command=self.apply)
        self.apply_button.pack(side='right')
        self.cancel_button = ttk.Button(buttons, text='Cancel', command=self.cancel)
        self.cancel_button.pack(side='right', padx=8)
        self.update_mode()
        self.grab_set()

    def build_order(self, selected):
        editor = ttk.Frame(self.controls)
        editor.pack(fill='x')
        self.column = tk.StringVar(value=self.columns[0] if self.columns else '')
        self.direction = tk.StringVar(value='ASC')
        ttk.Combobox(editor, textvariable=self.column, values=self.columns, state='readonly',
                     width=28).pack(side='left', fill='x', expand=True)
        ttk.Combobox(editor, textvariable=self.direction, values=('ASC', 'DESC'), state='readonly',
                     width=6).pack(side='left', padx=6)
        ttk.Button(editor, text='Add', command=self.add_order).pack(side='left')
        list_frame = ttk.Frame(self.controls)
        list_frame.pack(fill='both', expand=True, pady=8)
        self.order_list = ttk.Treeview(list_frame, columns=('column', 'direction'),
                                      show='headings', selectmode='browse', height=8)
        self.order_list.heading('column', text='Column (first row sorts first)')
        self.order_list.heading('direction', text='Direction')
        self.order_list.column('direction', width=85, stretch=False)
        vertical = ttk.Scrollbar(list_frame, orient='vertical', command=self.order_list.yview)
        horizontal = ttk.Scrollbar(list_frame, orient='horizontal', command=self.order_list.xview)
        self.order_list.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.order_list.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.order_list.bind('<<TreeviewSelect>>', self.select_order)
        for name, direction in selected:
            self.order_list.insert('', 'end', values=(name, direction))
        actions = ttk.Frame(self.controls)
        actions.pack(fill='x')
        for label, command in [('Update', self.update_order), ('Remove', self.remove_order),
                               ('Move up', lambda: self.move_order(-1)),
                               ('Move down', lambda: self.move_order(1))]:
            ttk.Button(actions, text=label, command=command).pack(side='left', padx=(0, 5))

    def select_order(self, event=None):
        selected = self.order_list.selection()
        if selected:
            name, direction = self.order_list.item(selected[0], 'values')
            self.column.set(name)
            self.direction.set(direction)

    def add_order(self):
        self.write_order()

    def update_order(self):
        selected = self.order_list.selection()
        if selected:
            self.write_order(selected[0])

    def write_order(self, item=None):
        name = self.column.get()
        if name not in self.columns:
            self.error.set('Choose an available column.')
            return
        if any(self.order_list.item(child, 'values')[0] == name
               for child in self.order_list.get_children() if child != item):
            self.error.set('That column is already in the sort order.')
            return
        values = (name, self.direction.get())
        if item is None:
            item = self.order_list.insert('', 'end', values=values)
        else:
            self.order_list.item(item, values=values)
        self.order_list.selection_set(item)
        self.order_list.see(item)
        self.error.set('')

    def remove_order(self):
        for item in self.order_list.selection():
            self.order_list.delete(item)

    def move_order(self, offset):
        selected = self.order_list.selection()
        if selected:
            item = selected[0]
            index = max(0, min(len(self.order_list.get_children()) - 1,
                               self.order_list.index(item) + offset))
            self.order_list.move(item, '', index)
            self.order_list.see(item)

    def update_mode(self):
        enabled = self.mode.get() == 'choose' and not self.saving
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Button, ttk.Combobox)):
                    child.configure(state=('readonly' if isinstance(child, ttk.Combobox) else 'normal')
                                    if enabled else 'disabled')
                visit(child)
        visit(self.controls)

    def apply(self):
        if self.saving:
            return
        choices = None
        if self.mode.get() == 'choose':
            choices = tuple(tuple(self.order_list.item(item, 'values'))
                            for item in self.order_list.get_children())
            if not choices:
                self.error.set('Choose at least one column.')
                return
        self.saving = True
        self.apply_button.configure(state='disabled')
        self.cancel_button.configure(state='disabled')
        self.update_mode()
        self.error.set('Saving…')
        self.apply_callback(self, choices)

    def failed(self, error):
        self.saving = False
        self.error.set(str(error))
        self.apply_button.configure(state='normal')
        self.cancel_button.configure(state='normal')
        self.update_mode()

    def cancel(self):
        if not self.saving:
            self.destroy()

    def destroy(self):
        super().destroy()
        self.mode = self.error = self.column = self.direction = None
