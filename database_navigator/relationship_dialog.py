"""Custom relationship editor. Database work is supplied as asynchronous callbacks."""

import tkinter as tk
from tkinter import messagebox, ttk


def notation(key):
    return (f"{key.source_schema}.{key.source_table}({', '.join(key.source_columns)}) → "
            f"{key.target_schema}.{key.target_table}({', '.join(key.target_columns)})")


class RelationshipDialog(tk.Toplevel):
    def __init__(self, parent, model, source, service, request, changed):
        super().__init__(parent)
        self.request = request
        self.changed = changed
        self.source = source
        self.service = service
        self.records = {}
        self.identifier = None
        self.busy = False
        self.loaded = False
        self.columns = ((), ())
        self.controls = []
        self.title(f'Custom relationships — {model}')
        self.transient(parent.winfo_toplevel())
        self.geometry('880x680')
        self.minsize(720, 600)
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.bind('<Escape>', lambda event: self.cancel())
        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=f'Custom relationships for model: {model}',
                  font=('TkDefaultFont', 12, 'bold')).pack(anchor='w')
        ttk.Label(body, text='Stored locally and used in both directions. Database foreign keys are unchanged.'
                  ).pack(anchor='w', pady=(4, 8))
        self.relationships = self.tree(body, ('relationship',), ('Relationship',), 5)
        self.relationships.column('relationship', width=800)
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=6)
        self.button(actions, 'Add', self.new)
        self.edit_button = self.button(actions, 'Edit', self.edit)
        self.delete_button = self.button(actions, 'Delete', self.delete)
        self.button(actions, 'Reload list', self.reload)
        self.relationships.bind('<<TreeviewSelect>>', lambda event: self.update_controls())
        self.relationships.bind('<Double-1>', lambda event: self.edit())

        self.editor_title = tk.StringVar()
        editor = ttk.LabelFrame(body, text='Relationship definition', padding=10)
        editor.pack(fill='both', expand=True, pady=6)
        ttk.Label(editor, textvariable=self.editor_title).pack(anchor='w')
        endpoints = ttk.Frame(editor)
        endpoints.pack(fill='x', pady=6)
        self.endpoints = []
        for index, label in enumerate(('Source (foreign-key columns)', 'Referenced table')):
            frame = ttk.Frame(endpoints)
            frame.grid(row=0, column=index, sticky='ew', padx=(0, 12) if index == 0 else 0)
            endpoints.columnconfigure(index, weight=1)
            ttk.Label(frame, text=label).grid(row=0, column=0, columnspan=2, sticky='w')
            variables = []
            for row, name in enumerate(('Schema', 'Table'), 1):
                ttk.Label(frame, text=name).grid(row=row, column=0, sticky='w', padx=(0, 6))
                variable = tk.StringVar()
                entry = ttk.Entry(frame, textvariable=variable)
                entry.grid(row=row, column=1, sticky='ew', pady=2)
                self.controls.append((entry, 'normal'))
                variable.trace_add('write', self.invalidate_columns)
                variables.append(variable)
            frame.columnconfigure(1, weight=1)
            self.endpoints.append(variables)
        load_bar = ttk.Frame(editor)
        load_bar.pack(fill='x')
        self.button(load_bar, 'Load columns', self.load_columns)
        ttk.Label(load_bar, text=('Blank schema uses the connection search path.' if service.supports_schemas else
                                 'Use schema main. Enter table names without SQL quotes.')
                  ).pack(side='left', padx=6)
        self.pairs = self.tree(editor, ('source', 'target'), ('Source column', 'Referenced column'), 4)
        selectors = ttk.Frame(editor)
        selectors.pack(fill='x', pady=6)
        self.column_vars = [tk.StringVar(), tk.StringVar()]
        self.combos = []
        for variable in self.column_vars:
            combo = ttk.Combobox(selectors, textvariable=variable, state='readonly', width=24)
            combo.pack(side='left', fill='x', expand=True, padx=(0, 6))
            self.combos.append(combo)
        self.pair_add = self.button(selectors, 'Add pair', self.add_pair)
        self.pair_update = self.button(selectors, 'Update pair', self.update_pair)
        pair_actions = ttk.Frame(editor)
        pair_actions.pack(fill='x')
        self.button(pair_actions, 'Remove pair', self.remove_pair)
        self.button(pair_actions, 'Move up', lambda: self.move_pair(-1))
        self.button(pair_actions, 'Move down', lambda: self.move_pair(1))
        self.pairs.bind('<<TreeviewSelect>>', self.select_pair)
        self.error = tk.StringVar()
        ttk.Label(body, textvariable=self.error, wraplength=820).pack(fill='x', pady=6)
        footer = ttk.Frame(body)
        footer.pack(fill='x')
        self.save_button = self.button(footer, 'Save relationship', self.save)
        self.close_button = self.button(footer, 'Close', self.cancel)
        self.new()
        self.reload()
        self.grab_set()

    @staticmethod
    def tree(parent, columns, headings, height):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, pady=(6, 0))
        tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse', height=height)
        for column, heading in zip(columns, headings):
            tree.heading(column, text=heading)
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        return tree

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side='left', padx=(0, 6))
        self.controls.append((button, 'normal'))
        return button

    def update_controls(self):
        for widget, normal in self.controls:
            widget.configure(state='disabled' if self.busy else normal)
        selected = bool(self.relationships.selection())
        for button in (self.edit_button, self.delete_button):
            button.configure(state='normal' if selected and not self.busy else 'disabled')
        for widget in (*self.combos, self.pair_add, self.pair_update, self.save_button):
            widget.configure(state=('readonly' if widget in self.combos else 'normal')
                             if self.loaded and not self.busy else 'disabled')

    def run(self, operation, args, completed, message):
        self.busy = True
        self.error.set(message)
        self.update_controls()
        def ready(result):
            self.busy = False
            self.error.set('')
            self.update_controls()
            completed(result)
        self.request(self, operation, args, ready, self.failed)

    def failed(self, error):
        self.busy = False
        self.error.set(str(error))
        self.update_controls()

    def reload(self, selected=None):
        if self.busy:
            return
        def ready(records):
            self.records = dict(records)
            self.relationships.delete(*self.relationships.get_children())
            for identifier, key in records:
                self.relationships.insert('', 'end', iid=str(identifier), values=(notation(key),))
            if selected in self.records:
                self.relationships.selection_set(str(selected))
                self.relationships.see(str(selected))
            self.update_controls()
        self.run(self.service.custom_relationships, (), ready, 'Loading custom relationships…')

    def invalidate_columns(self, *args):
        self.loaded = False
        # Traces can run while the form is being constructed.
        if hasattr(self, 'save_button'):
            self.update_controls()

    def new(self):
        if self.busy:
            return
        self.identifier = None
        self.editor_title.set('New relationship')
        self.endpoints[0][0].set(self.source[0] or '')
        self.endpoints[0][1].set(self.source[1])
        self.endpoints[1][0].set(self.source[0] or ('' if self.service.supports_schemas else 'main'))
        self.endpoints[1][1].set('')
        self.pairs.delete(*self.pairs.get_children())
        for variable in self.column_vars:
            variable.set('')
        self.error.set('Enter both tables, then load columns and add column pairs.')
        self.update_controls()

    def edit(self):
        selected = self.relationships.selection()
        if self.busy or not selected:
            return
        self.identifier = int(selected[0])
        key = self.records[self.identifier]
        self.editor_title.set('Editing custom relationship')
        for variables, values in zip(self.endpoints, ((key.source_schema, key.source_table),
                                                      (key.target_schema, key.target_table))):
            for variable, value in zip(variables, values):
                variable.set(value)
        self.pairs.delete(*self.pairs.get_children())
        for pair in zip(key.source_columns, key.target_columns):
            self.pairs.insert('', 'end', values=pair)
        self.load_columns()

    def load_columns(self):
        if self.busy:
            return
        endpoints = [tuple(variable.get() for variable in variables) for variables in self.endpoints]
        if any(not table for _, table in endpoints):
            self.error.set('Enter both table names first.')
            return
        self.loaded = False
        def source_ready(source):
            def target_ready(target):
                for variables, endpoint in zip(self.endpoints, (source, target)):
                    for variable, value in zip(variables, endpoint[:2]):
                        variable.set(value)
                self.columns = (source[2], target[2])
                for combo, variable, columns in zip(self.combos, self.column_vars, self.columns):
                    combo.configure(values=columns)
                    variable.set(columns[0] if columns else '')
                self.loaded = True
                self.update_controls()
                self.error.set('Choose column pairs in order, then save.')
            self.run(self.service.relationship_columns, endpoints[1], target_ready, 'Loading referenced columns…')
        self.run(self.service.relationship_columns, endpoints[0], source_ready, 'Loading source columns…')

    def select_pair(self, event=None):
        if self.busy:
            return
        selected = self.pairs.selection()
        if selected:
            for variable, value in zip(self.column_vars, self.pairs.item(selected[0], 'values')):
                variable.set(value)

    def add_pair(self, item=None):
        if self.busy or not self.loaded:
            return
        pair = tuple(variable.get() for variable in self.column_vars)
        if any(value not in columns for value, columns in zip(pair, self.columns)):
            self.error.set('Choose an available column on each side.')
            return
        for child in self.pairs.get_children():
            if child != item:
                existing = self.pairs.item(child, 'values')
                if any(a == b for a, b in zip(pair, existing)):
                    self.error.set('Use each source and referenced column only once.')
                    return
        if item is None:
            item = self.pairs.insert('', 'end', values=pair)
        else:
            self.pairs.item(item, values=pair)
        self.pairs.selection_set(item)
        self.pairs.see(item)
        self.error.set('')

    def update_pair(self):
        selected = self.pairs.selection()
        if selected:
            self.add_pair(selected[0])

    def remove_pair(self):
        if not self.busy:
            self.pairs.delete(*self.pairs.selection())

    def move_pair(self, offset):
        selected = self.pairs.selection()
        if selected and not self.busy:
            item = selected[0]
            index = max(0, min(len(self.pairs.get_children()) - 1, self.pairs.index(item) + offset))
            self.pairs.move(item, '', index)
            self.pairs.see(item)

    def save(self):
        if self.busy or not self.loaded:
            return
        pairs = [self.pairs.item(item, 'values') for item in self.pairs.get_children()]
        if not pairs:
            self.error.set('Add at least one column pair.')
            return
        endpoints = {}
        for index, side in enumerate(('source', 'target')):
            schema, table = (variable.get() for variable in self.endpoints[index])
            endpoints[side] = {'schema': schema, 'table': table, 'columns': [pair[index] for pair in pairs]}
        def saved(identifier):
            self.identifier = identifier
            self.editor_title.set('Editing custom relationship')
            self.changed()
            self.reload(identifier)
        self.run(self.service.save_custom_relationship, (self.identifier, {'relationships': [endpoints]}),
                 saved, 'Validating and saving relationship…')

    def delete(self):
        selected = self.relationships.selection()
        if self.busy or not selected:
            return
        identifier = int(selected[0])
        if not messagebox.askyesno('Delete custom relationship',
                                  f'Delete this declaration from the current model?\n\n'
                                  f'{notation(self.records[identifier])}', parent=self):
            return
        def deleted(_):
            if self.identifier == identifier:
                self.new()
            self.changed()
            self.reload()
        self.run(self.service.delete_custom_relationship, (identifier,), deleted, 'Deleting custom relationship…')

    def cancel(self):
        if not self.busy:
            self.destroy()
