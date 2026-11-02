from __future__ import annotations

import getpass
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import main
from .session import Pacing
from .timers import ProcessTimers


# meiva:note
# The operation identity's ambient half, read once here and passed down as values (task #6024):
# the id is minted per invocation, never derived from a time or a host (two windows started in
# the same second are still two operations), and the initiator names who ran this — a fleet
# window refuses to run inside an agent's shell, so an initiator here is always a person at a
# machine, and `main` re-checks that refusal itself.
def _initiator() -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = str(os.getuid())
    return f"user:{user}@{socket.gethostname()}"


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1:],
            repo=Path(__file__).resolve().parents[2],
            env=os.environ,
            now=datetime.now(UTC),
            pacing=Pacing(),
            timers=ProcessTimers(),
            operation_id=uuid4().hex,
            initiator=_initiator(),
        )
    )
