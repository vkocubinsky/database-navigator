"""A one-hop relationship diagram. Drawing and selection perform no database I/O."""

import tkinter as tk
from tkinter import ttk


class DiagramDialog(tk.Toplevel):
    def __init__(self, parent, select, table_key):
        super().__init__(parent)
        self.title('Relationship diagram')
        self.geometry('1000x680')
        self.minsize(600, 400)
        self.transient(parent.winfo_toplevel())
        self.select = select
        self.table_key = table_key
        self.snapshot = None
        self.selected = None
        self.master_identity = None
        self.links = ()
        self.columns = {}
        self.bind('<Escape>', lambda event: self.destroy())
        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Direct parents and children only · Arrows point to the referenced table',
                  wraplength=560).pack(anchor='w')
        ttk.Label(body, text='Click an arrow or column mapping to show related rows.',
                  wraplength=560).pack(anchor='w', pady=(4, 0))
        ttk.Label(body, text='Boxes show primary-key columns (PK) and columns used by the shown relationships.',
                  wraplength=560).pack(anchor='w', pady=(4, 10))
        area = ttk.Frame(body)
        area.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(area, background='#f5f7fb', highlightthickness=0,
                                xscrollincrement=1, yscrollincrement=1)
        window_system = self.tk.call('tk', 'windowingsystem')
        tk9 = self.tk.call('package', 'vcompare', self.tk.call('package', 'provide', 'Tk'), '9.0') >= 0
        self._wheel_scale = 24 / (1 if window_system == 'aqua' and not tk9 else 120)
        self._wheel_remainder = [0.0, 0.0]
        self.canvas.bind('<MouseWheel>', self._scroll_wheel)
        self.canvas.bind('<Shift-MouseWheel>', self._scroll_wheel)
        if tk9:
            self.canvas.bind('<TouchpadScroll>', self._scroll_touchpad)
        if window_system == 'x11':
            for button, pixels in ((4, -24), (5, 24)):
                self.canvas.bind(f'<Button-{button}>',
                                 lambda event, step=pixels: self._scroll(
                                     step if event.state & 1 else 0, 0 if event.state & 1 else step))
        vertical = ttk.Scrollbar(area, orient='vertical', command=self.canvas.yview)
        horizontal = ttk.Scrollbar(area, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.canvas.bind('<Configure>', self._resize)
        ttk.Button(body, text='Close', command=self.destroy).pack(anchor='e', pady=(8, 0))
        self.message('Loading relationships…')

    def message(self, text):
        self.master_identity = None
        self.links = ()
        self.columns = {}
        self.canvas.delete('all')
        self.canvas.create_text(24, 24, text=text, anchor='nw', width=520,
                                fill='#334155', font='TkDefaultFont')
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def show(self, master, links, columns, selected=None):
        self.master_identity, self.links, self.selected = master, tuple(links), selected
        self.columns = columns
        self.title(f'Relationship diagram — {master[0]}.{master[1]}')
        self.draw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def highlight(self, selected):
        self.selected = selected
        for index in range(len(self.links)):
            color = '#b45309' if index == selected else '#2563a6'
            self.canvas.itemconfigure(f'link:{index}', fill=color)

    def _resize(self, event):
        if self.master_identity is not None:
            self.draw()

    def _scroll(self, horizontal, vertical):
        # Canvas items share these widget-local bindings; other dialogs are unaffected.
        if horizontal:
            self.canvas.xview_scroll(horizontal, 'units')
        if vertical:
            self.canvas.yview_scroll(vertical, 'units')
        return 'break'

    def _scroll_wheel(self, event):
        axis = 0 if event.state & 1 else 1
        self._wheel_remainder[axis] -= event.delta * self._wheel_scale
        pixels = int(self._wheel_remainder[axis])
        self._wheel_remainder[axis] -= pixels
        return self._scroll(pixels if axis == 0 else 0, pixels if axis == 1 else 0)

    def _scroll_touchpad(self, event):
        # Tk 9 packs signed deltas for both axes into %D, including momentum.
        horizontal, vertical = self.tk.call('tk::PreciseScrollDeltas', event.delta)
        return self._scroll(-int(horizontal), -int(vertical))

    def _text(self, x, y, text, *, width=240, bold=False, tags=()):
        item = self.canvas.create_text(x, y, text=text, anchor='nw', width=width,
                                       fill='#233247', tags=tags,
                                       font=('TkDefaultFont', 11, 'bold' if bold else 'normal'))
        return self.canvas.bbox(item)[3] - y

    def _mapping(self, index):
        link = self.links[index]
        source, target = ((link.selected_columns, link.columns) if link.direction == 'parent'
                          else (link.columns, link.selected_columns))
        return '\n'.join(f'{left} → {right}' for left, right in zip(source, target))

    def _bind_link(self, index):
        tag = f'link:{index}'
        self.canvas.tag_bind(tag, '<Button-1>', lambda event: self.select(index))
        self.canvas.tag_bind(tag, '<Enter>', lambda event: self.canvas.configure(cursor='hand2'))
        self.canvas.tag_bind(tag, '<Leave>', lambda event: self.canvas.configure(cursor=''))

    def _card(self, x, y, title, key, *, master=False, minimum_bottom=0):
        top = y
        y += self._text(x + 14, y + 12, title, bold=True) + 24
        self.canvas.create_line(x + 1, y - 6, x + 269, y - 6, fill='#b7c6d8')
        for column, primary in self.columns.get(key, ()):
            height = self._text(x + 14, y, f'PK  {column}' if primary else column,
                                tags=('column',))
            y += height + 6
        y += 8
        y = max(y, minimum_bottom)
        box = self.canvas.create_rectangle(x, top, x + 270, y,
                                            fill='#deedff' if master else 'white',
                                            outline='#245c93' if master else '#b7c6d8',
                                            width=2 if master else 1,
                                            tags=('master',) if master else ('table',))
        self.canvas.tag_lower(box)
        return y

    def _label(self, x, y, index):
        """Keep clickable mappings outside cards, with a clear background."""
        tag = f'link:{index}'
        height = self._text(x, y, self._mapping(index), tags=(tag, 'mapping'))
        text = self.canvas.find_withtag('mapping')[-1]
        left, top, right, bottom = self.canvas.bbox(text)
        background = self.canvas.create_rectangle(left - 3, top - 3, right + 3, bottom + 3,
                                                 fill='#f5f7fb', outline='', tags=('mapping-background',))
        self.canvas.tag_lower(background, text)
        self.canvas.tag_bind(background, '<Button-1>', lambda event: self.select(index))
        return height

    def draw(self):
        c = self.canvas
        c.delete('all')
        master_key = self.table_key(*self.master_identity)
        groups = {'parent': {}, 'child': {}}
        self_links = []
        for index, link in enumerate(self.links):
            key = self.table_key(link.schema, link.table)
            if key == master_key:
                self_links.append(index)
            else:
                groups[link.direction].setdefault(key, []).append(index)
        count = max(len(groups['parent']), len(groups['child']), 1)
        width = max(c.winfo_width(), count * 300 + 48, 860 if self_links else 640)
        center = width / 2

        def row(direction, y):
            self._text(24, y, 'Parent tables' if direction == 'parent' else 'Child tables', bold=True)
            y += 32
            entries = list(groups[direction].values())
            bottom, anchors = y + 28, []
            if not entries:
                qualifier = 'other ' if self_links else ''
                self._text(24, y, f'No {qualifier}direct {direction} tables')
            positions = [center - len(entries) * 300 / 2 + position * 300 + 15
                         for position in range(len(entries))]

            def labels(top):
                end = top
                for x, indices in zip(positions, entries):
                    cursor = top
                    for index in indices:
                        cursor += self._label(x + 14, cursor, index) + 18
                    end = max(end, cursor)
                return end

            if direction == 'child' and entries:
                y = labels(y) + 12
            for x, indices in zip(positions, entries):
                link = self.links[indices[0]]
                title = f'{link.schema or self.master_identity[0]}.{link.table}'
                end = self._card(x, y, title, self.table_key(link.schema, link.table))
                bottom = max(bottom, end)
                for port, index in enumerate(indices):
                    anchors.append((index, x + 270 * (port + 1) / (len(indices) + 1),
                                    end if direction == 'parent' else y))
            if direction == 'parent' and entries:
                bottom = labels(bottom + 18)
            return bottom, anchors

        parent_bottom, parent_ports = row('parent', 20)
        master_top = parent_bottom + 60
        # Reserve the right side of the master band for labelled self loops.
        master_x = center - (280 if self_links else 135)
        master_title = f'{self.master_identity[0]}.{self.master_identity[1]}'
        loop_bottom = master_top
        for index in self_links:
            label_y = loop_bottom + 12
            tag = f'link:{index}'
            height = self._text(master_x + 310, label_y,
                                f'Self-reference · {self.links[index].direction}\n{self._mapping(index)}',
                                tags=(tag,))
            end = label_y + height
            c.create_line(master_x + 270, label_y, master_x + 290, label_y,
                          master_x + 290, end, master_x + 270, end,
                          arrow='last', width=2, tags=(tag, 'self-loop'))
            loop_bottom = end + 12
        master_bottom = self._card(master_x, master_top, f'Master table\n{master_title}', master_key,
                                   master=True, minimum_bottom=loop_bottom)
        child_top = master_bottom + 60
        bottom, child_ports = row('child', child_top)
        for ports, parent in ((parent_ports, True), (child_ports, False)):
            for position, (index, x, y) in enumerate(ports):
                port_x = master_x + 270 * (position + 1) / (len(ports) + 1)
                coords = ((port_x, master_top, x, y) if parent
                          else (x, y, port_x, master_bottom))
                edge = c.create_line(*coords, arrow='last', arrowshape=(10, 12, 5),
                                     width=2, tags=(f'link:{index}', 'edge'))
                c.tag_lower(edge)
        for index in range(len(self.links)):
            self._bind_link(index)
        self.highlight(self.selected)
        c.configure(scrollregion=(0, 0, width, bottom + 30))
