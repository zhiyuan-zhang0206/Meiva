# meiva:note
# Which port the frontend application listens on, as a function of the two settings that name
# it. It is NOT the entry port: the entry is owned by the always-up gate, which answers 200
# even while the app is down, so probing the entry would make a dead app look alive.

from __future__ import annotations

__all__ = ["application_port"]

_DEFAULT_ENTRY_PORT = 3000


# meiva:note
# `MEIVA_APP_PORT` when set, else the entry port (the port of the entry health URL, 3000 when it
# names none) plus one.
def application_port(*, entry_port: int | None, app_port: int | None) -> int:
    return app_port or ((entry_port or _DEFAULT_ENTRY_PORT) + 1)
