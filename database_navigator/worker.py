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

from collections import deque
from concurrent.futures import Future
from queue import Empty, Queue
from threading import Event, Lock, Thread


__all__ = ["DatabasePool", "PendingTasks"]


class DatabasePool:
    def __init__(self, session_factory, max_workers=4):
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("Worker count must be a positive integer.")
        self.session_factory = session_factory
        self.jobs = Queue()
        self.stopping = Event()
        self._lock = Lock()
        self.threads = [Thread(target=self._run, name=f"database-reader-{i + 1}", daemon=True)
                        for i in range(max_workers)]
        for thread in self.threads:
            thread.start()

    def submit(self, operation, *args):
        """Run a named session method or a callable taking (session, *args)."""
        future = Future()
        with self._lock:
            if self.stopping.is_set():
                future.cancel()
            else:
                self.jobs.put((future, operation, args))
        return future

    def close(self):
        cancelled = []
        with self._lock:
            if self.stopping.is_set():
                return
            self.stopping.set()
            while True:
                try:
                    future, _, _ = self.jobs.get_nowait()
                except Empty:
                    break
                cancelled.append(future)
            for _ in self.threads:
                self.jobs.put(None)
        # Future callbacks may call submit/close; never run them under our lock.
        for future in cancelled:
            future.cancel()

    def _run(self):
        session = None
        try:
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                future, operation, args = job
                with self._lock:
                    stopping = self.stopping.is_set()
                    running = not stopping and future.set_running_or_notify_cancel()
                if stopping:
                    future.cancel()
                if not running:
                    continue
                try:
                    if session is None:
                        session = self.session_factory()
                    if isinstance(operation, str):
                        result = getattr(session, operation)(*args)
                    else:
                        result = operation(session, *args)
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)
        finally:
            if session is not None:
                session.close()


class PendingTasks:
    """Poll futures and deliver callbacks on the owning thread, with ordered saves."""

    def __init__(self):
        self.closed = False
        self.pending = []
        self._ordered_jobs = deque()
        self._ordered_job = None

    @property
    def busy(self):
        return (bool(self._ordered_jobs) or self._ordered_job is not None
                or any(current() for _, _, _, current in self.pending))

    def submit(self, operation, args, completed, failed, current=lambda: True):
        future = operation(*args)
        self.pending.append((future, completed, failed, current))
        return future

    def cancel_reads(self, *, stale_only=False):
        """Cancel pending reads where possible. Ordered operations have their own lifetime."""
        for future, _, _, current in self.pending:
            if not stale_only or not current():
                future.cancel()

    def queue_ordered(self, operation, args, completed, failed, current=lambda: True):
        # Saves outlive navigation. New defaults reads must follow earlier saves.
        self._ordered_jobs.append((operation, args, completed, failed, current))

    @staticmethod
    def _deliver(future, completed, failed):
        try:
            result = future.result()
        except Exception as exc:
            failed(exc)
        else:
            completed(result)

    def _poll_ordered(self):
        if self._ordered_job is not None:
            future, completed, failed, current = self._ordered_job
            if not future.done():
                return
            self._ordered_job = None
            if current():
                self._deliver(future, completed, failed)
        while not self.closed and self._ordered_jobs and self._ordered_job is None:
            operation, args, completed, failed, current = self._ordered_jobs.popleft()
            if current():
                self._ordered_job = (operation(*args), completed, failed, current)

    def poll(self):
        if self.closed:
            return
        self._poll_ordered()
        pending, self.pending = self.pending, []
        for future, completed, failed, current in pending:
            if self.closed or not current():
                future.cancel()
            elif not future.done():
                self.pending.append((future, completed, failed, current))
            else:
                self._deliver(future, completed, failed)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_reads()
        self.pending.clear()
        self._ordered_jobs.clear()
        if self._ordered_job is not None:
            self._ordered_job[0].cancel()
            self._ordered_job = None
