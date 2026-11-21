# meiva:note
# Static child programs for native E2E port ownership tests.

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from tests.e2e._ports import FRONTEND_RESERVATION, reserve_health_port, reserve_port


def main() -> None:
    command, *arguments = sys.argv[1:]
    if command == "exit":
        raise SystemExit(17)
    if command == "hold":
        time.sleep(30)
    elif command == "listen":
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", int(arguments[0])))
            sock.listen()
            time.sleep(30)
    elif command == "descendant":
        # meiva:note
        # Fixed fixture module; argv contains only the test port.
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "tests.e2e.fakes.port_server", "listen", arguments[0]],
            check=True,
        )
    elif command == "lease":
        reservations = [
            FRONTEND_RESERVATION,
            reserve_port(),
            reserve_health_port("agent-host"),
            reserve_health_port("ops"),
        ]
        for reservation in reservations:
            reservation.release()
        Path(arguments[0]).write_text(json.dumps([r.port for r in reservations]))
        time.sleep(30)
    else:
        raise ValueError(f"unknown port test command: {command!r}")


if __name__ == "__main__":
    main()
