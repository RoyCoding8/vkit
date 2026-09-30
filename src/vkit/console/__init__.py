"""A local browser console over the vkit core.

Plan 05. Local-only, single user, no accounts, no HTTPS. The server binds
127.0.0.1 and refuses any other address at the socket, because it has no
authentication and is safe only while no other machine can reach it.

The dependency direction is the design. `operations` knows nothing about HTTP,
`server` knows nothing about installing, and `api` validates at the boundary.
`plan` holds the one list of what may be written, and nothing outside it writes.
"""
from __future__ import annotations

from .operations import Context, open_context
from .plan import WRITABLE, WRITABLE_NAMES, NotImplementedInBuild, Refused
from .server import BindRefused, serve, start_in_thread

__all__ = [
    "BindRefused",
    "Context",
    "NotImplementedInBuild",
    "Refused",
    "WRITABLE",
    "WRITABLE_NAMES",
    "open_context",
    "serve",
    "start_in_thread",
]
