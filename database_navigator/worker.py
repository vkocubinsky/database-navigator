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

"""Bounded database thread pool, with one private connection per worker."""

from concurrent.futures import Future
from queue import Empty, Queue
from threading import Event, Lock, Thread

from .metadata import MetadataCache


class DatabasePool:
    def __init__(self, browser_factory, max_workers=4, *, metadata=None):
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("Worker count must be a positive integer.")
        self.metadata = metadata if metadata is not None else MetadataCache()
        self.browser_factory = browser_factory
        self.jobs = Queue()
        self.stopping = Event()
        self._lock = Lock()
        self.threads = [Thread(target=self._run, name=f"database-reader-{i + 1}", daemon=True)
                        for i in range(max_workers)]
        for thread in self.threads:
            thread.start()

    def submit(self, method, *args):
        future = Future()
        with self._lock:
            if self.stopping.is_set():
                future.cancel()
            else:
                self.jobs.put((future, method, args, self.metadata.generation))
        return future

    def close(self):
        cancelled = []
        with self._lock:
            if self.stopping.is_set():
                return
            self.stopping.set()
            while True:
                try:
                    future, _, _, _ = self.jobs.get_nowait()
                except Empty:
                    break
                cancelled.append(future)
            for _ in self.threads:
                self.jobs.put(None)
        # Future callbacks may call submit/close; never run them under our lock.
        for future in cancelled:
            future.cancel()

    def _run(self):
        browser = None
        try:
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                future, method, args, generation = job
                with self._lock:
                    stopping = self.stopping.is_set()
                    running = not stopping and future.set_running_or_notify_cancel()
                if stopping:
                    future.cancel()
                if not running:
                    continue
                try:
                    if browser is None:
                        browser = self.browser_factory()
                    if method == "relationships":
                        result = self.metadata.relationships(browser, *args, generation)
                    elif method == 'relationship_diagram':
                        result = self.metadata.relationship_diagram(browser, *args, generation)
                    else:
                        result = getattr(browser, method)(*args)
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)
        finally:
            if browser is not None:
                browser.close()
