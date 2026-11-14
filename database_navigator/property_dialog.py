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

"""Column and ordering editors; all persistence is delegated to the caller."""

import tkinter as tk
from tkinter import ttk

from .models import parse_columns


class PropertyDialog(tk.Toplevel):
    def __init__(self, parent, link, property_name, columns, saved, apply):
        super().__init__(parent)
        self.property_name = property_name
        self.apply_callback = apply
        self.columns = columns
        self.saving = False
        ordering = property_name == 'order_by'
        self.title(f'{"Order" if ordering else "Columns"} — {link.display_table}')
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
        self.mode = tk.StringVar(value='choose' if property_name in saved else 'default')
        for label, value in ((('No order' if ordering else 'All'), 'default'), ('Choose', 'choose')):
            ttk.Radiobutton(mode_bar, text=label, value=value, variable=self.mode,
                            command=self.update_mode).pack(side='left', padx=(0, 12))
        self.error = tk.StringVar()
        try:
            selected = parse_columns(saved.get(property_name), ordering=ordering)
        except ValueError as exc:
            selected = ()
            self.error.set(str(exc))
        self.controls = ttk.Frame(body)
        self.controls.pack(fill='both', expand=True)
        if ordering:
            self.build_order(selected)
        else:
            if property_name not in saved:
                selected = self.columns
            self.build_columns(selected)
            ttk.Label(body, text='Include key columns if you want to navigate further from this table.',
                      wraplength=510).pack(anchor='w', pady=(8, 0))
        ttk.Label(body, textvariable=self.error, wraplength=510).pack(fill='x', pady=8)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x')
        self.apply_button = ttk.Button(buttons, text='Apply', command=self.apply)
        self.apply_button.pack(side='right')
        self.cancel_button = ttk.Button(buttons, text='Cancel', command=self.cancel)
        self.cancel_button.pack(side='right', padx=8)
        self.update_mode()
        self.grab_set()

    def build_columns(self, selected):
        canvas = self.column_canvas = tk.Canvas(self.controls, highlightthickness=0, yscrollincrement=1)
        window_system = self.tk.call('tk', 'windowingsystem')
        tk9 = self.tk.call('package', 'vcompare', self.tk.call('package', 'provide', 'Tk'), '9.0') >= 0
        self._wheel_scale = 24 / (1 if window_system == 'aqua' and not tk9 else 120)
        self._wheel_remainder = 0.0
        scroll = ttk.Scrollbar(self.controls, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        self.checks = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.checks, anchor='nw')
        self.checks.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        self.column_vars = {}
        for name in self.columns:
            variable = tk.BooleanVar(value=name in selected)
            self.column_vars[name] = variable
            ttk.Checkbutton(self.checks, text=name, variable=variable).pack(anchor='w', pady=2)
        # Bind only this list and its children, never all application windows.
        for widget in (canvas, self.checks, *self.checks.winfo_children()):
            widget.bind('<MouseWheel>', self.scroll_columns_wheel)
            if tk9:
                widget.bind('<TouchpadScroll>', self.scroll_columns_touchpad)
            if window_system == 'x11':
                widget.bind('<Button-4>', lambda event: self.scroll_columns(-24))
                widget.bind('<Button-5>', lambda event: self.scroll_columns(24))
        missing = set(selected) - set(self.columns)
        if missing:
            self.error.set('Unavailable saved columns: ' + ', '.join(sorted(missing)))

    def scroll_columns(self, pixels):
        self.column_canvas.yview_scroll(pixels, 'units')
        return 'break'

    def scroll_columns_wheel(self, event):
        if event.state & 1:  # Shift-wheel is horizontal, not vertical scrolling.
            return 'break'
        self._wheel_remainder -= event.delta * self._wheel_scale
        pixels = int(self._wheel_remainder)
        self._wheel_remainder -= pixels
        return self.scroll_columns(pixels)

    def scroll_columns_touchpad(self, event):
        # Tk 9 packs both axes into %D. Its decoder preserves the signed deltas
        # supplied by macOS, including natural scrolling and momentum.
        _, vertical = self.tk.call('tk::PreciseScrollDeltas', event.delta)
        return self.scroll_columns(-int(vertical))

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
                if isinstance(child, (ttk.Button, ttk.Checkbutton, ttk.Combobox)):
                    child.configure(state=('readonly' if isinstance(child, ttk.Combobox) else 'normal')
                                    if enabled else 'disabled')
                visit(child)
        visit(self.controls)

    def apply(self):
        if self.saving:
            return
        choices = None
        if self.mode.get() == 'choose':
            choices = (tuple(tuple(self.order_list.item(item, 'values'))
                             for item in self.order_list.get_children())
                       if self.property_name == 'order_by' else
                       tuple(name for name, variable in self.column_vars.items() if variable.get()))
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
