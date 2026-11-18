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

"""Column projection editor; persistence is delegated to the caller."""

import tkinter as tk
from tkinter import ttk


__all__ = ["ColumnsDialog"]


class ColumnsDialog(tk.Toplevel):
    def __init__(self, parent, link, columns, selected, apply, *, error=''):
        super().__init__(parent)
        self.apply_callback = apply
        self.columns = columns
        self.saving = False
        self.title(f'Columns — {link.display_table}')
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
        for label, value in (('All', 'default'), ('Choose', 'choose')):
            ttk.Radiobutton(mode_bar, text=label, value=value, variable=self.mode,
                            command=self.update_mode).pack(side='left', padx=(0, 12))
        self.error = tk.StringVar(value=error)
        self.controls = ttk.Frame(body)
        self.controls.pack(fill='both', expand=True)
        self.build_columns(self.columns if selected is None else selected)
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

    def update_mode(self):
        enabled = self.mode.get() == 'choose' and not self.saving
        for check in self.checks.winfo_children():
            check.configure(state='normal' if enabled else 'disabled')

    def apply(self):
        if self.saving:
            return
        choices = None
        if self.mode.get() == 'choose':
            choices = tuple(name for name, variable in self.column_vars.items() if variable.get())
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
        # Finalize Tcl variables on the UI thread even if Python collects the
        # destroyed widget tree later on a database worker.
        self.column_vars.clear()
        self.mode = self.error = None
