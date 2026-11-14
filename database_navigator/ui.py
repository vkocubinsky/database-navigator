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

"""Tkinter query results and navigation through directly related tables."""

import argparse
import tkinter as tk
import time
from tkinter import ttk

from . import __version__
from .controller import NavigationController
from .models import DEFAULT_LIMIT, validate_limit
from .service import DEFAULT_DATABASE, open_service
from .property_dialog import PropertyDialog
from .relationship_dialog import RelationshipDialog
from .diagram_dialog import DiagramDialog


class ResultGrid(ttk.Frame):
    def __init__(self, parent, height=5):
        super().__init__(parent)
        self.tree = ttk.Treeview(self, show="headings", selectmode="browse", height=height)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def show(self, columns, rows):
        self.tree.delete(*self.tree.get_children())
        ids = [str(i) for i in range(len(columns))]
        self.tree.configure(columns=ids)
        for identifier, name in zip(ids, columns):
            self.tree.heading(identifier, text=name)
            self.tree.column(identifier, width=130, minwidth=70, stretch=False)
        for index, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(index), values=[
                "NULL" if value is None else str(value) for value in row
            ])


class RelatedContent(ttk.Frame):
    """Content for the single selected relationship."""

    def __init__(self, parent, link, selected_table, open_button):
        super().__init__(parent, padding=8, relief="groove", borderwidth=1)
        self.link = link
        self.loading = False
        self.revision = 0
        self.future = None
        self.primary_key = None
        self.primary_key_error = None
        self.grid = None
        self.columns, self.rows = (), []
        self.row_selection = ()
        self.row_selection_revision = 0
        header = ttk.Frame(self)
        header.pack(fill="x")
        self.open_button = open_button
        ttk.Label(header, text=f"Table: {link.display_table}").pack(side="left")
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, pady=(6, 0))
        ttk.Label(self.body, text=link.notation(selected_table), wraplength=440).pack(anchor="w")
        self.open_row_hint = ttk.Label(self.body, text='Open row: reading primary key…', wraplength=440)
        self.open_row_hint.pack(side='bottom', anchor='w')
        self.content = ttk.Frame(self.body)
        self.content.pack(fill="both", expand=True)

    def cancel_read(self):
        self.revision += 1
        self.loading = False
        if self.future is not None:
            self.future.cancel()
            self.future = None

    def clear(self, message=None):
        self.grid = None
        self.columns, self.rows = (), []
        self.row_selection = ()
        self.row_selection_revision += 1
        for widget in self.content.winfo_children():
            widget.destroy()
        if message:
            ttk.Label(self.content, text=message, wraplength=440).pack(anchor="w", pady=5)

    def show_result(self, result, limit):
        self.clear()
        columns, rows = self.columns, self.rows = result
        grid = self.grid = ResultGrid(self.content, height=12)
        grid.pack(fill="both", expand=True, pady=5)
        grid.show(columns, rows)
        ttk.Label(self.content, text=f"{len(rows)} rows fetched · limit {limit}").pack(anchor="w")


class Navigator(ttk.Frame):
    def __init__(self, root, service):
        super().__init__(root, padding=12)
        self.service = service
        self.controller = NavigationController(service)
        self.model = service.model
        self.property_dialog = None
        self.relationship_dialog = None
        self.help_dialog = None
        self.diagram_dialog = None
        self._property_request = 0
        self._links_ready = False
        self._relationship_error = None
        self.related_content = None
        self.related_index = None
        self._last_selection = None
        self._stage = "Ready. Press Send to execute the query."
        self._stage_started = time.monotonic()
        self.columns, self.rows, self.links = (), [], []
        self.pack(fill="both", expand=True)
        root.title(f"Database Navigator {__version__} — {service.label} (read-only)")
        root.geometry("1200x800")
        root.minsize(800, 550)
        # Ask the window manager for its usable desktop area, preserving the
        # Dock/taskbar and menu bar instead of entering native full-screen mode.
        try:
            if root.tk.call('tk', 'windowingsystem') in ('aqua', 'win32'):
                root.state('zoomed')
            else:
                root.attributes('-zoomed', True)
        except tk.TclError:
            # Keep the normal, resizable geometry if maximization is unsupported.
            pass

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        self.back_button = ttk.Button(toolbar, text="Back", state="disabled", command=lambda: self.move_history(-1))
        self.back_button.pack(side="left", padx=(12, 4))
        self.forward_button = ttk.Button(toolbar, text="Forward", state="disabled", command=lambda: self.move_history(1))
        self.forward_button.pack(side="left")
        self.reset_metadata_button = ttk.Button(toolbar, text="Reset cache", command=self.reset_metadata)
        self.reset_metadata_button.pack(side="left", padx=8)
        self.relationships_button = ttk.Button(
            toolbar, text='Relationships…', command=self.manage_relationships,
            state='normal' if self.model else 'disabled')
        self.relationships_button.pack(side='left', padx=(0, 8))
        self.diagram_button = ttk.Button(toolbar, text='Diagram', state='disabled', command=self.show_diagram)
        self.diagram_button.pack(side='left', padx=(0, 8))
        self.help_button = ttk.Button(toolbar, text='Help', command=self.show_help)
        self.help_button.pack(side='left')
        self.limit = tk.StringVar(value=str(DEFAULT_LIMIT))
        self.limit_box = ttk.Spinbox(toolbar, from_=1, to=10000, width=6,
                                     textvariable=self.limit, command=self.change_limit)
        self.limit_box.pack(side="right", padx=8)
        self.limit_box.bind("<Return>", self.change_limit)
        self.limit_box.bind("<FocusOut>", self.change_limit)
        ttk.Label(toolbar, text="Row limit (each table)").pack(side="right")
        self.status = tk.StringVar(value="Ready. Press Send to execute the query.")
        ttk.Label(self, textvariable=self.status, wraplength=1100).pack(fill="x", pady=8)

        panes = self.main_panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        panes.bind('<Map>', self.initialize_main_split)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=2)
        panes.add(right, weight=3)
        master = ttk.Panedwindow(left, orient="vertical")
        master.pack(fill="both", expand=True)
        query_pane = ttk.Frame(master)
        master.add(query_pane, weight=0)
        query_header = ttk.Frame(query_pane)
        query_header.pack(fill="x", pady=(0, 6))
        ttk.Button(query_header, text="Send", command=self.send_from_button).pack(side="right", padx=(0, 16))
        self.save_startup_button = ttk.Button(query_header, text='Set as startup',
                                              state='disabled', command=self.save_startup_sql)
        self.save_startup_button.pack(side='right', padx=(6, 8))
        ttk.Label(query_header, text="Free SQL", width=1,
                  font=("TkDefaultFont", 12, "bold")).pack(side="left", fill='x', expand=True)
        navigation_hint = ttk.Label(
            query_pane,
            text='For complex SQL, choose the navigation table with -- derived: schema.table',
            font='TkSmallCaptionFont', width=1, wraplength=440, justify='left')
        navigation_hint.pack(side='bottom', fill='x', padx=(0, 16), pady=(6, 0))
        navigation_hint.bind('<Configure>', lambda event: navigation_hint.configure(
            wraplength=max(1, event.width)))
        editor_frame = ttk.Frame(query_pane)
        editor_frame.pack(fill="both", expand=True)
        self.editor = tk.Text(editor_frame, height=8, width=1, wrap="none", undo=True, font="TkFixedFont")
        editor_y = ttk.Scrollbar(editor_frame, orient="vertical", command=self.editor.yview)
        editor_x = ttk.Scrollbar(editor_frame, orient="horizontal", command=self.editor.xview)
        self.editor.configure(yscrollcommand=editor_y.set, xscrollcommand=editor_x.set)
        self.editor.grid(row=0, column=0, sticky="nsew")
        editor_y.grid(row=0, column=1, sticky="ns")
        editor_x.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        self._editor_revision = self._editor_focus_revision = 0
        self.editor.edit_modified(False)
        self.editor.bind('<<Modified>>', self.editor_modified)
        self.editor.bind('<FocusOut>', self.editor_focus_out)
        self.editor.bind("<Control-Return>", self.send)
        self.editor.bind("<Command-Return>", self.send)
        browse_shortcut = '<Option-Return>' if root.tk.call('tk', 'windowingsystem') == 'aqua' else '<Alt-Return>'
        self.editor.bind(browse_shortcut, lambda event: self.send(event, focus_results=True))
        rows_pane = ttk.Frame(master)
        master.add(rows_pane, weight=1)
        ttk.Label(rows_pane, text="Master rows", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(8, 0))
        self.results = ResultGrid(rows_pane, height=20)
        self.results.pack(fill="both", expand=True, pady=(8, 0))
        self.results.tree.bind("<<TreeviewSelect>>", self.select_row)
        self.bind_grid_shortcuts(self.results.tree)
        details = self.details = ttk.Panedwindow(right, orient="vertical")
        details.pack(fill="both", expand=True)
        navigation = ttk.Frame(details)
        details.add(navigation, weight=1)
        ttk.Label(navigation, text="Linked tables", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        direction_bar = ttk.Frame(navigation)
        direction_bar.pack(fill="x", pady=(8, 0))
        action_bar = ttk.Frame(navigation)
        action_bar.pack(fill='x', pady=(8, 0))
        self.open_button = ttk.Button(action_bar, text="Open table", state="disabled",
                                      command=lambda: self.open_related(self.related_content))
        self.open_button.pack(side="right", padx=(6, 0))
        self.open_row_button = ttk.Button(action_bar, text='Open row', state='disabled',
                                          command=self.open_row)
        self.open_row_button.pack(side='right', padx=(6, 0))
        self.columns_button = ttk.Button(action_bar, text='Columns', state='disabled',
                                         command=lambda: self.edit_property('columns'))
        self.columns_button.pack(side='right', padx=(6, 0))
        self.order_button = ttk.Button(action_bar, text='Order', state='disabled',
                                       command=lambda: self.edit_property('order_by'))
        self.order_button.pack(side='right', padx=(6, 0))
        ttk.Label(direction_bar, text="Show:").pack(side="left", padx=(0, 6))
        self.related_direction = tk.StringVar(value="Child")
        self.direction_buttons = []
        for direction in ("Any", "Child", "Parent"):
            button = ttk.Radiobutton(direction_bar, text=direction, value=direction,
                                     variable=self.related_direction, state='disabled')
            button.pack(side="left", padx=(0, 6))
            self.direction_buttons.append(button)
        self.related_direction.trace_add("write", self.change_direction)
        search_bar = ttk.Frame(navigation)
        search_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(search_bar, text="Search tables").pack(side="left", padx=(0, 6))
        self.related_search = tk.StringVar()
        self.search_entry = ttk.Entry(search_bar, textvariable=self.related_search)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.related_search.trace_add("write", self.filter_related)
        list_frame = ttk.Frame(navigation)
        list_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.related_list = ttk.Treeview(list_frame, columns=("relationship",),
                                         show="headings", selectmode="browse", height=6)
        self.related_list.heading("relationship", text="Relationship")
        self.related_list.column("relationship", width=440)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.related_list.yview)
        horizontal = ttk.Scrollbar(list_frame, orient='horizontal', command=self.related_list.xview)
        self.related_list.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.related_list.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.related_list.bind("<<TreeviewSelect>>", self.select_related)
        self.related_list.bind('<ButtonRelease-1>', self.confirm_related_selection)
        self.related_list.bind('<Return>', self.confirm_related_selection)
        self.bind_grid_shortcuts(self.related_list)
        self.sections = ttk.Frame(details)
        details.add(self.sections, weight=3)
        self.generated_pane = ttk.Frame(details)
        details.add(self.generated_pane, weight=0)
        sql_header = ttk.Frame(self.generated_pane)
        sql_header.pack(fill="x", pady=(6, 4))
        self.generated_title = tk.StringVar(value="Generated SQL")
        self.generated_toggle = ttk.Button(sql_header, text="Hide", width=5, command=self.toggle_generated_sql)
        self.generated_toggle.pack(side="right", padx=(6, 0))
        self.copy_sql_button = ttk.Button(sql_header, text="Copy", state="disabled", command=self.copy_generated_sql)
        self.copy_sql_button.pack(side="right")
        ttk.Label(sql_header, textvariable=self.generated_title, width=1, anchor="w").pack(
            side="left", fill="x", expand=True)
        self.generated_body = ttk.Frame(self.generated_pane)
        self.generated_body.pack(fill="both", expand=True)
        self.generated_sql = tk.Text(self.generated_body, height=4, width=1, wrap="none",
                                     state="disabled", font="TkFixedFont")
        sql_y = ttk.Scrollbar(self.generated_body, orient="vertical", command=self.generated_sql.yview)
        sql_x = ttk.Scrollbar(self.generated_body, orient="horizontal", command=self.generated_sql.xview)
        self.generated_sql.configure(yscrollcommand=sql_y.set, xscrollcommand=sql_x.set)
        self.generated_sql.grid(row=0, column=0, sticky="nsew")
        sql_y.grid(row=0, column=1, sticky="ns")
        sql_x.grid(row=1, column=0, sticky="ew")
        self.generated_body.rowconfigure(0, weight=1)
        self.generated_body.columnconfigure(0, weight=1)
        self._generated_visible = True
        self._generated_height = 130
        self._poll_id = self.after(40, self._poll)
        if self.model:
            self._set_stage(f'Loading startup SQL for {self.model}…')
            self.controller.queue_ordered(self.service.startup_sql, (), self.startup_sql_ready, self.startup_sql_failed)
        self.editor.focus_set()

    def show_diagram(self):
        if not self._links_ready:
            return
        if self.diagram_dialog is None or not self.diagram_dialog.winfo_exists():
            self.diagram_dialog = DiagramDialog(self, self.select_diagram_link, self.service.table_key)
        self.diagram_dialog.lift()
        self.update_diagram()

    def update_diagram(self, message=None):
        self.diagram_button.configure(state='normal' if self._links_ready else 'disabled')
        dialog = self.diagram_dialog
        if dialog is None or not dialog.winfo_exists():
            return
        if not self._links_ready:
            dialog.snapshot = None
            dialog.message(message or 'Loading the current table’s relationships…')
            return
        # Keep the same drawing while rows or the selected relationship change.
        snapshot = self.links
        if dialog.snapshot is snapshot:
            dialog.highlight(self.related_index)
            return
        dialog.snapshot = snapshot
        dialog.message('Loading relationships…')
        table = self.service.base_table(self.controller.visit.query.statement)

        def current():
            return (not self.controller.closed and bool(dialog.winfo_exists())
                    and dialog.snapshot is snapshot and self._links_ready)

        def failed(exc):
            dialog.snapshot = None
            dialog.message(f'Could not load diagram: {exc}')

        self.controller.submit(
            self.service.relationship_diagram, (table,),
            lambda result: dialog.show(*result, self.related_index),
            failed, current)

    def select_diagram_link(self, index):
        if not self._links_ready or not 0 <= index < len(self.links):
            return
        # A diagram always shows both directions. Reveal a clicked link even if
        # the list's temporary search or direction filter currently hides it.
        if not self.related_list.exists(str(index)):
            self._links_ready = False
            self.related_direction.set('Any')
            self.controller.set_filters(direction='Any', search='')
            self.related_search.set('')
            self._links_ready = True
            self.populate_related()
        self.related_list.selection_set(str(index))
        self.related_list.focus(str(index))
        self.related_list.see(str(index))
        self.save_navigation_default(self.links[index])
        if self.related_index != index:
            self.select_related()

    def show_help(self):
        if self.help_dialog is not None and self.help_dialog.winfo_exists():
            self.help_dialog.lift()
            self.help_dialog.focus_set()
            return
        dialog = self.help_dialog = tk.Toplevel(self)
        dialog.title('Keyboard shortcuts')
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(False, False)
        dialog.bind('<Escape>', lambda event: dialog.destroy())
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill='both', expand=True)
        mac = self.tk.call('tk', 'windowingsystem') == 'aqua'
        groups = (
            ('Master rows, Relationships, Details', (
                ('n / ↓', 'Next row'),
                ('p / ↑', 'Previous row'),
                ('c', 'Next pane: Master rows → Relationships → Details → Master rows'),
                ('Shift+C', 'Previous pane in the same cycle'),
                ('b', 'Back in navigation history'),
                ('f', 'Forward in navigation history'),
                ('e', 'Return to Free SQL at the current cursor position'),
            )),
            ('Details only', (
                ('o', 'Open the selected row using its primary key'),
                ('Ctrl+o / Command+o' if mac else 'Ctrl+o',
                 'Open table using the current relationship'),
            )),
            ('Free SQL', (
                ('Ctrl+Enter / Command+Enter' if mac else 'Ctrl+Enter',
                 'Execute SQL and stay in the editor'),
                ('Option+Enter' if mac else 'Alt+Enter',
                 'Execute SQL and focus Master rows when results arrive'),
            )),
        )
        row = 0
        for title, shortcuts in groups:
            ttk.Label(body, text=title, font=('TkDefaultFont', 12, 'bold')).grid(
                row=row, column=0, columnspan=2, sticky='w', pady=(12 if row else 0, 6))
            row += 1
            for keys, description in shortcuts:
                ttk.Label(body, text=keys).grid(row=row, column=0, sticky='nw', padx=(0, 20), pady=3)
                ttk.Label(body, text=description, wraplength=350, justify='left').grid(
                    row=row, column=1, sticky='w', pady=3)
                row += 1
        ttk.Label(body, text='Letter shortcuts apply only in the grids; text fields keep normal typing.\n'
                  'Open row requires a selected row with all primary-key columns available.',
                  wraplength=580, justify='left').grid(
                      row=row, column=0, columnspan=2, sticky='w', pady=(14, 10))
        close = ttk.Button(body, text='Close', command=dialog.destroy)
        close.grid(row=row + 1, column=0, columnspan=2, sticky='e')
        close.focus_set()

    def editor_modified(self, event=None):
        if self.editor.edit_modified():
            self._editor_revision += 1
            self.editor.edit_modified(False)

    def editor_focus_out(self, event):
        self._editor_focus_revision += 1

    def bind_grid_shortcuts(self, tree):
        for key in ('c', 'C', 'e'):
            tree.bind(f'<Key-{key}>', self.move_grid_focus)
        for key in ('n', 'p'):
            tree.bind(f'<Key-{key}>', self.move_grid_row)
        for key in ('b', 'f'):
            tree.bind(f'<Key-{key}>', self.move_grid_history)

    def move_grid_history(self, event):
        if self.grid_shortcut_modified(event):
            return
        button = self.back_button if event.keysym == 'b' else self.forward_button
        button.invoke()
        return 'break'

    def grid_shortcut_modified(self, event):
        # Leave modified shortcuts alone; Mod2 is Num Lock outside macOS.
        modifiers = 0x4 | 0x8 | 0x40 | 0x80
        if self.tk.call('tk', 'windowingsystem') == 'aqua':
            modifiers |= 0x10 | 0x20
        return bool(event.state & modifiers)

    def move_grid_row(self, event):
        if self.grid_shortcut_modified(event):
            return
        event.widget.event_generate('<Down>' if event.keysym == 'n' else '<Up>')
        return 'break'

    def move_grid_focus(self, event):
        if self.grid_shortcut_modified(event):
            return
        if event.keysym == 'e':
            self.editor.focus_set()
            return 'break'
        direction = 1 if event.keysym == 'c' else -1
        section = self.related_content
        detail_tree = section.grid.tree if section is not None and section.grid is not None else None
        panes = (self.results.tree, self.related_list, detail_tree)
        if event.widget in panes:
            index = panes.index(event.widget)
            for step in range(1, len(panes) + 1):
                target = panes[(index + step * direction) % len(panes)]
                if (target is not None and target.winfo_ismapped()
                        and not target.instate(['disabled']) and target.get_children()):
                    if target is detail_tree:
                        selected = target.selection()
                        row = selected[0] if selected else target.get_children()[0]
                        if not selected:
                            target.selection_set(row)
                        target.focus(row)
                        target.see(row)
                    target.focus_set()
                    break
        return 'break'

    def bind_detail_shortcuts(self, tree):
        tree.bind('<Key-o>', self.open_from_details)
        tree.bind('<Control-o>', lambda event: self.open_from_details(event, row=False))
        if self.tk.call('tk', 'windowingsystem') == 'aqua':
            tree.bind('<Command-o>', lambda event: self.open_from_details(event, row=False))

    def open_from_details(self, event, *, row=True):
        if row and self.grid_shortcut_modified(event):
            return
        section = self.related_content
        if section is not None and section.grid is not None and event.widget is section.grid.tree:
            button = self.open_row_button if row else self.open_button
            button.invoke()
        return 'break'

    def startup_sql_ready(self, text):
        if (self._editor_revision == 0 and not self.editor.edit_modified()
                and self.controller.history_index < 0 and not self.editor.get('1.0', 'end-1c')):
            self.editor.insert('1.0', text)
            self.editor.edit_reset()
        self.save_startup_button.configure(state='normal')
        if self.controller.history_index < 0:
            self._set_stage('Ready. Press Send to execute the query.')

    def startup_sql_failed(self, error):
        self.save_startup_button.configure(state='normal')
        self._set_stage(f'Could not load startup SQL: {error}')

    def save_startup_sql(self):
        if not self.model or self.save_startup_button.instate(['disabled']):
            return
        self.editor.focus_set()
        text = self.editor.get('1.0', 'end-1c')
        self.save_startup_button.configure(state='disabled')
        self._set_stage(f'Saving startup SQL for {self.model}…')

        def completed(_):
            self.save_startup_button.configure(state='normal')
            action = 'saved' if text.strip() else 'cleared'
            self._set_stage(f'Startup SQL {action} for {self.model}.')

        def failed(error):
            self.save_startup_button.configure(state='normal')
            self._set_stage(f'Could not save startup SQL: {error}')

        self.controller.queue_ordered(self.service.save_startup_sql, (text,), completed, failed)

    def initialize_main_split(self, event):
        # Pane weights govern resizing, but initial widths otherwise come from
        # each pane's requested size. Set the first sash after layout is mapped.
        self.main_panes.unbind('<Map>')
        self.after_idle(lambda: self.main_panes.sashpos(0, self.main_panes.winfo_width() * 2 // 5))

    @property
    def busy(self):
        return self.controller.busy

    def _set_stage(self, text):
        self._stage = text
        self._stage_started = time.monotonic()
        self.status.set(text)

    def _poll(self):
        self.controller.poll()
        if self.busy:
            elapsed = time.monotonic() - self._stage_started
            self.status.set(f"{self._stage} ({elapsed:.1f}s)")
        self._poll_id = self.after(40, self._poll)

    def destroy(self):
        if not self.controller.closed:
            self.after_cancel(self._poll_id)
            self.controller.close()
        super().destroy()

    def row_limit(self):
        try:
            limit = int(self.limit.get())
        except ValueError:
            raise ValueError("Row limit must be an integer between 1 and 10000.") from None
        validate_limit(limit)
        return limit

    def show_generated_sql(self, query=None, table=None):
        text = query.text if query is not None else ''
        self.generated_sql.configure(state='normal')
        self.generated_sql.delete('1.0', 'end')
        self.generated_sql.insert('1.0', text)
        self.generated_sql.configure(state='disabled')
        self.generated_title.set(f'Generated SQL — {table}' if table else 'Generated SQL')
        self.copy_sql_button.configure(state='normal' if text else 'disabled')

    def copy_generated_sql(self):
        text = self.generated_sql.get('1.0', 'end-1c')
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def toggle_generated_sql(self):
        if self._generated_visible:
            self._generated_height = self.generated_pane.winfo_height()
            self.generated_body.pack_forget()
            self.generated_toggle.configure(text='Show')
        else:
            self.generated_body.pack(fill='both', expand=True)
            self.generated_toggle.configure(text='Hide')
        self._generated_visible = not self._generated_visible
        self.update_idletasks()
        height = self._generated_height if self._generated_visible else self.generated_pane.winfo_reqheight()
        self.details.sashpos(1, max(self.details.sashpos(0) + 80, self.details.winfo_height() - height))

    def clear_content(self):
        self.show_generated_sql()
        self.open_button.configure(state="disabled")
        self.open_row_button.configure(state='disabled')
        self.columns_button.configure(state='disabled')
        self.order_button.configure(state='disabled')
        if self.related_content is not None:
            self.related_content.cancel_read()
        self.related_content = None
        for widget in self.sections.winfo_children():
            widget.destroy()

    def clear_sections(self):
        for button in self.direction_buttons:
            button.configure(state='disabled')
        self.clear_content()
        self.related_index = None
        self.related_list.delete(*self.related_list.get_children())

    def capture_visit(self):
        selected = self.results.tree.selection()
        try:
            limit = self.row_limit()
        except ValueError:
            limit = None
        self.controller.capture_visit(
            search=self.related_search.get(), direction=self.related_direction.get(),
            selected_index=int(selected[0]) if selected else None,
            related_index=self.related_index, links_ready=self._links_ready, limit=limit)

    def history_buttons(self):
        self.back_button.configure(state="normal" if self.controller.can_move(-1) else "disabled")
        self.forward_button.configure(state="normal" if self.controller.can_move(1) else "disabled")

    def move_history(self, offset):
        if not self.controller.can_move(offset):
            return
        self.capture_visit()
        self.activate_visit(self.controller.move_history(offset))
        self.results.tree.focus_set()

    def open_related(self, section):
        selected = self.results.tree.selection()
        if section is None or not selected or not self.rows:
            self._set_stage("Select a row before opening a related table.")
            return
        try:
            args = (section.link, self.columns, self.rows[int(selected[0])], self.row_limit())
        except ValueError as exc:
            self._set_stage(f"Error: {exc}")
            return
        self.editor_modified()
        editor_revision = self._editor_revision
        current = lambda: section is self.related_content

        def prepared(query):
            if self._editor_revision != editor_revision or self.editor.edit_modified():
                self._set_stage('Open table cancelled because Free SQL changed.')
                return
            self.send(navigation=query)

        self._set_stage(f'Preparing {section.link.display_table}…')
        self.controller.open_related(*args, prepared=prepared,
                                     failed=lambda exc: self._set_stage(f'Error: {exc}'), current=current)

    def update_open_row(self, event=None):
        section = self.related_content
        self.open_row_button.configure(state='disabled')
        if section is None:
            return
        selected = section.grid.tree.selection() if section.grid is not None else ()
        if selected != section.row_selection:
            section.row_selection = selected
            section.row_selection_revision += 1
        if section.primary_key_error:
            reason = section.primary_key_error
        elif section.primary_key is None:
            reason = 'Reading primary key…'
        elif not section.primary_key:
            reason = 'This table has no declared primary key.'
        elif not selected:
            reason = 'Select a related row to open it.'
        else:
            try:
                self.service.validate_row_key(section.primary_key, section.columns,
                                              section.rows[int(selected[0])])
            except ValueError as exc:
                reason = str(exc)
            else:
                reason = ''
                self.open_row_button.configure(state='normal')
        section.open_row_hint.configure(text=f'Open row: {reason}' if reason else '')

    def load_primary_key(self, section):
        current = lambda: section is self.related_content

        def ready(key):
            section.primary_key = key
            self.update_open_row()

        def failed(exc):
            section.primary_key_error = f'Could not read primary key: {exc}'
            self.update_open_row()

        self.controller.load_primary_key(section.link, completed=ready, failed=failed, current=current)

    def open_row(self):
        section = self.related_content
        self.update_open_row()
        if section is None or self.open_row_button.instate(['disabled']):
            return
        selected = section.grid.tree.selection()
        try:
            args = (section.link, section.columns, section.rows[int(selected[0])], self.row_limit())
        except ValueError as exc:
            self._set_stage(f'Error: {exc}')
            return
        revision = section.revision
        row_revision = section.row_selection_revision
        self.editor_modified()
        editor_revision = self._editor_revision
        current = lambda: (section is self.related_content and revision == section.revision
                           and row_revision == section.row_selection_revision
                           and section.grid is not None and section.grid.tree.selection() == selected)

        def prepared(query):
            if self._editor_revision != editor_revision or self.editor.edit_modified():
                self._set_stage('Open row cancelled because Free SQL changed.')
                return
            self.send(navigation=query)

        self._set_stage(f'Preparing selected row from {section.link.display_table}…')
        self.controller.open_row(*args, prepared=prepared,
                                 failed=lambda exc: self._set_stage(f'Error: {exc}'), current=current)

    def manage_relationships(self):
        if not self.model:
            return
        if self.relationship_dialog is not None and self.relationship_dialog.winfo_exists():
            self.relationship_dialog.lift()
            return
        visit = self.controller.visit
        source = self.service.relationship_source(visit.query if visit else None)

        def request(dialog, operation, args, completed, failed):
            # Use the ordered local-storage queue so navigation never cancels a save.
            self.controller.queue_ordered(operation, args, completed, failed,
                                lambda: not self.controller.closed and bool(dialog.winfo_exists()))

        self.relationship_dialog = RelationshipDialog(
            self, self.model, source, self.service, request, self.reset_metadata)

    def edit_property(self, property_name):
        section = self.related_content
        if not self.model or section is None:
            return
        if self.property_dialog is not None and self.property_dialog.winfo_exists():
            self.property_dialog.lift()
            return
        self._property_request += 1
        token = self._property_request
        current = lambda: (not self.controller.closed and token == self._property_request
                           and section is self.related_content)
        self._set_stage(f'Loading columns for {section.link.display_table}…')

        def ready(result):
            columns, saved = result
            self.related_status()
            self.property_dialog = PropertyDialog(
                self, section.link, property_name, columns, saved, save)

        def save(dialog, choices):
            def completed(_):
                dialog.destroy()
                # Refresh only this selection; never rewrite free SQL or main results.
                if section is self.related_content:
                    self.select_row()
            self.controller.queue_ordered(self.service.save_preference, (section.link, property_name, choices),
                         completed, dialog.failed,
                         lambda: not self.controller.closed and bool(dialog.winfo_exists()))

        self.controller.submit(self.service.table_preferences, (section.link,), ready,
                     lambda exc: self._set_stage(f'Error: {exc}'), current)

    def send_from_button(self):
        self.editor.focus_set()
        return self.send()

    def send(self, event=None, *, navigation=None, focus_results=False):
        try:
            limit = self.row_limit()
            query = self.controller.query_for_text(self.editor.get("1.0", "end-1c"), navigation)
        except ValueError as exc:
            self._set_stage(f"Error: {exc}")
            return "break"
        self.capture_visit()
        visit = self.controller.add_visit(query, limit)
        self.activate_visit(visit, focus_results=focus_results)
        if navigation is not None:
            self.results.tree.focus_set()
        return "break"

    def reset_metadata(self):
        self.capture_visit()
        self.controller.reset_metadata()
        visit = self.controller.visit
        if visit is not None and visit.rows is not None:
            # Reuse main rows and preserve unsent editor changes.
            text = self.editor.get("1.0", "end-1c")
            self.activate_visit(visit)
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", text)
        elif visit is None:
            self._set_stage("Metadata cache cleared. Press Send to execute the query.")

    def activate_visit(self, visit, *, focus_results=False):
        self.controller.begin_visit()
        self._last_selection = None
        self.clear_sections()
        self.columns, self.rows, self.links = (), [], []
        self._links_ready = False
        self.update_diagram()
        self.related_search.set(visit.search)
        self.related_direction.set(visit.direction)
        self._relationship_error = None
        self.results.show((), [])
        # Sending the current SQL must not move the caret or disturb selection,
        # scrolling, or undo. Replace editor text only when navigating to other SQL.
        if self.editor.get('1.0', 'end-1c') != visit.query.text:
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", visit.query.text)
        self.limit.set(str(visit.limit))
        self.history_buttons()
        table = self.service.base_table(visit.query.statement)
        self.editor_modified()
        editor_revision, focus_revision = self._editor_revision, self._editor_focus_revision

        def metadata_failed(exc):
            self._relationship_error = f"Relationship discovery failed: {exc}"
            self._set_stage(self._relationship_error)
            self.clear_sections()
            ttk.Label(self.sections, text=self._relationship_error, wraplength=440).pack(anchor="w")
            self.update_diagram(self._relationship_error)

        def metadata_ready(links):
            self.links = links
            self._links_ready = True
            self.update_diagram()
            self.clear_sections()
            self.populate_related()
            for button in self.direction_buttons:
                button.configure(state='normal')
            if visit.related_index is not None:
                candidate = str(visit.related_index)
                self.related_list.selection_set(candidate)
                self.related_list.focus(candidate)
                self.related_list.see(candidate)
            self.select_related()

        def rows_ready(result):
            self.columns, self.rows = result
            self.results.show(self.columns, self.rows)
            if not self.rows:
                self._set_stage(f"{table}: no rows found.")
                ttk.Label(self.sections, text="No rows selected.").pack(anchor="w")
                self.update_diagram(f'{table}: no rows found. Relationships were not loaded.')
                return
            if visit.selected_index is not None:
                index = min(visit.selected_index, len(self.rows) - 1)
                self.results.tree.selection_set(str(index))
                self.results.tree.focus(str(index))
                self.results.tree.see(str(index))
                if (focus_results and self.focus_get() is self.editor
                        and focus_revision == self._editor_focus_revision
                        and editor_revision == self._editor_revision and not self.editor.edit_modified()):
                    self.results.tree.focus_set()

        def query_failed(exc):
            self._set_stage(f'Error: {exc}')
            self.update_diagram(f'Query failed: {exc}')

        self.controller.load_visit(
            visit, rows_ready=rows_ready, links_ready=metadata_ready,
            defaults_ready=self.related_direction.set,
            query_failed=query_failed,
            metadata_failed=metadata_failed, status=self._set_stage)

    def change_limit(self, event=None):
        try:
            self.row_limit()
        except ValueError as exc:
            self._set_stage(str(exc))
            return
        self.select_row()

    def select_row(self, event=None):
        signature = (self.results.tree.selection(), self.related_index, self.limit.get())
        if event is not None and signature == self._last_selection:
            return
        self._last_selection = signature
        self.controller.change_selection()
        if not self._links_ready:
            self.clear_sections()
            if self.results.tree.selection() and self.rows:
                text = self._relationship_error or "Discovering relationships…"
                ttk.Label(self.sections, text=text, wraplength=440).pack(anchor="w")
            return
        section = self.related_content
        if section is not None:
            self.open_row_button.configure(state='disabled')
            selected = bool(self.results.tree.selection() and self.rows)
            section.open_button.configure(state="normal" if selected else "disabled")
            section.cancel_read()
            section.clear()
            self.load_related(section)
        self.related_status(reset_timer=True)

    def populate_related(self):
        self.related_list.delete(*self.related_list.get_children())
        table = self.service.base_table(self.controller.visit.query.statement)
        for index in self.controller.visible_links(
                self.links, self.related_search.get(), self.related_direction.get()):
            self.related_list.insert("", "end", iid=str(index), values=(self.links[index].notation(table),))

    def filter_related(self, *_):
        self.controller.set_filters(search=self.related_search.get())
        if not self._links_ready or self.controller.closed:
            return
        index = self.related_index
        self.populate_related()
        if index is not None and self.related_list.exists(str(index)):
            self.related_list.selection_set(str(index))
            self.related_list.focus(str(index))
            self.related_list.see(str(index))
        else:
            self.select_related()

    def save_navigation_default(self, link=None):
        self.controller.save_navigation_default(
            self.related_direction.get(), link,
            lambda exc: self._set_stage(f'Could not save navigation default: {exc}'))

    def confirm_related_selection(self, event):
        # Clicking the already-highlighted automatic fallback is also a deliberate choice.
        if self.related_index is None:
            return
        if event.keysym != 'Return' and self.related_list.identify_row(event.y) != str(self.related_index):
            return
        self.save_navigation_default(self.links[self.related_index])

    def change_direction(self, *_):
        self.controller.set_filters(direction=self.related_direction.get())
        if not self._links_ready or self.controller.closed:
            return
        if not self.model:
            self.filter_related()
            return
        self.save_navigation_default()
        self.populate_related()
        index = self.controller.preferred_link(
            self.links, self.related_search.get(), self.related_direction.get())
        if index is not None:
            candidate = str(index)
            self.related_list.selection_set(candidate)
            self.related_list.focus(candidate)
            self.related_list.see(candidate)
        self.select_related()

    def select_related(self, event=None):
        if not self._links_ready:
            return
        selected = self.related_list.selection()
        index = int(selected[0]) if selected else None
        if event is not None and index == self.related_index:
            return
        if event is not None and index is not None:
            self.save_navigation_default(self.links[index])
        self.clear_content()
        self.related_index = index
        self.update_diagram()
        if index is not None:
            table = self.service.base_table(self.controller.visit.query.statement)
            self.related_content = RelatedContent(self.sections, self.links[index], table, self.open_button)
            self.related_content.pack(fill="both", expand=True, pady=(8, 0))
            self.load_primary_key(self.related_content)
            if self.model:
                self.columns_button.configure(state='normal')
                self.order_button.configure(state='normal')
        else:
            message = "Select a linked table." if self.links else "No related tables found."
            if self.links and not self.related_list.get_children():
                message = "No linked tables match your filters."
            ttk.Label(self.sections, text=message).pack(anchor="w", pady=8)
        self.select_row()

    def related_status(self, *, reset_timer=False):
        section = self.related_content
        loading = section is not None and section.loading
        if loading:
            text = f"Loading linked table {section.link.display_table}…"
        elif section is not None:
            text = f"{len(self.rows)} rows fetched. Linked table: {section.link.display_table}."
        elif self.links:
            text = f"{len(self.rows)} rows fetched. Select a linked table."
        else:
            text = f"{len(self.rows)} rows fetched. No related tables found."
        if reset_timer or not loading:
            self._set_stage(text)
        else:
            self._stage = text
            self.status.set(text)

    def load_related(self, section):
        self.show_generated_sql()
        selected = self.results.tree.selection()
        if not selected or not self.rows:
            section.clear("No row selected.")
            self.update_open_row()
            return
        try:
            limit = self.row_limit()
        except ValueError as exc:
            section.clear(str(exc))
            self.update_open_row()
            return
        row = self.rows[int(selected[0])]
        revision = section.revision
        current = lambda: (section is self.related_content
                           and revision == section.revision)
        section.clear("Loading…")
        section.loading = True
        self.update_open_row()

        def complete(result=None, error=None):
            section.loading = False
            section.future = None
            if error is not None:
                section.clear(str(error))
            else:
                section.show_result(result, limit)
                section.grid.tree.bind('<<TreeviewSelect>>', self.update_open_row)
                self.bind_grid_shortcuts(section.grid.tree)
                self.bind_detail_shortcuts(section.grid.tree)
            self.update_open_row()
            self.related_status()

        section.future = self.controller.load_related(
            section.link, self.columns, row, limit,
            prepared=lambda query: self.show_generated_sql(query, section.link.display_table),
            completed=complete, failed=lambda exc: complete(error=exc), current=current)


def main():
    parser = argparse.ArgumentParser(description="Read-only SQLite/PostgreSQL Database Navigator")
    parser.add_argument('--version', action='version', version=f'Database Navigator {__version__}')
    parser.add_argument("connection")
    parser.add_argument('--model', default='default',
                        help='Shared database model used for preferences and relationships (default: default)')
    parser.add_argument('--application-database', default=DEFAULT_DATABASE,
                        help='Local SQLite preferences file (default: database-navigator.sqlite)')
    args = parser.parse_args()
    try:
        service = open_service(args.connection, model=args.model,
                               application_database=args.application_database)
    except ValueError as exc:
        parser.exit(1, f"Error: {exc}\n")
    try:
        root = tk.Tk()
        Navigator(root, service)
        root.mainloop()
    finally:
        service.close()


if __name__ == "__main__":
    main()
