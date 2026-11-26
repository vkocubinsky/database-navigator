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

"""Connection arguments shared by the browser and the metadata CLI."""

import argparse
import re

from . import __version__


__all__ = ["connection_parser", "select_backend"]

_BACKENDS = ("sqlite", "postgresql")


def connection_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--version", action="version", version=f"Database Navigator {__version__}")
    parser.add_argument("--backend", choices=_BACKENDS,
                        help="Database engine (inferred from the connection when omitted)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--connection", help="SQLite file URL or PostgreSQL libpq connection string or URL")
    # Both forms set the same value. An absent positional must preserve the named one.
    source.add_argument("connection", nargs="?", default=argparse.SUPPRESS,
                        help="Positional form of --connection")
    return parser


def select_backend(connection: str, backend: str | None = None) -> str:
    """Resolve the engine without connecting or including credentials in errors."""
    if backend is not None and backend not in _BACKENDS:
        raise ValueError(f"Unsupported database backend: {backend}")
    match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", connection)
    if match:
        scheme = match.group(1)
        url_backend = "postgresql" if scheme == "postgres" else scheme
        if url_backend not in _BACKENDS:
            raise ValueError(f"Unsupported connection URL scheme: {scheme}")
        if backend is not None and backend != url_backend:
            raise ValueError(f"The {url_backend} connection URL conflicts with --backend {backend}.")
        backend = url_backend
    if backend == "sqlite" and not connection.startswith("sqlite:///"):
        raise ValueError("Use sqlite:///path/to/database for SQLite.")
    return backend or "postgresql"
