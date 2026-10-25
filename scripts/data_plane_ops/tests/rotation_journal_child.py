# meiva:note
# The actual CLI receives only a disposable journal. Networking is fenced before
# imports; writes are fenced after normal configuration preparation. An admitted
# bad record therefore fails the test without touching a service or another home.
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

_WRITE_EVENTS = frozenset(
    {
        "os.mkdir",
        "os.chmod",
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.link",
        "os.symlink",
        "os.truncate",
    }
)
_NETWORK_EVENTS = frozenset(
    {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}
)


def _snapshot(home: Path) -> dict[str, str]:
    return {
        str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in home.rglob("*")
        if p.is_file()
    }


def main() -> None:
    attempted: list[str] = []
    protect_writes = False

    def forbid_effect(event: str, args: tuple[object, ...]) -> None:
        if event in _NETWORK_EVENTS:
            attempted.append(event)
            raise PermissionError("journal input child forbids networking")
        if not protect_writes:
            return
        write_open = (
            event == "open"
            and isinstance(args[2], int)
            and bool(args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        )
        if event in _WRITE_EVENTS or write_open:
            attempted.append(event)
            raise PermissionError("journal input child forbids filesystem writes")

    sys.addaudithook(forbid_effect)
    from base.config import settings
    from scripts.data_plane_ops.rotate_data_plane_secrets import main as rotation_main

    # meiva:note
    # The real CLI reads these guards before journal admission. Prepare that existing
    # owner before the write fence, while the entire child is already network-fenced.
    if settings.profile == "agent" or settings.data_plane.is_remote:
        raise RuntimeError("journal input child needs its synthetic local gateway context")
    home = Path(os.environ["MEIVA_HOME"])
    before = _snapshot(home)
    protect_writes = True
    refused: str | None = None
    returned: int | None = None
    try:
        returned = rotation_main(["--execute", "--yes", "--resume", sys.argv[1]])
    except RuntimeError as exc:
        refused = str(exc)
    after = _snapshot(home)
    print(
        json.dumps(
            {
                "refused": refused,
                "returned": returned,
                "attempted": attempted,
                "home_unchanged": before == after,
            }
        )
    )


if __name__ == "__main__":
    main()
