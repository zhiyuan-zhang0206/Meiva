# meiva:note
# A fake of the session backend the manager converges, and the provenance it would report.
#
# `SessionBackend` is the manager's interface to the pty-sessions service. This fake holds the
# sessions a test names and records what the manager asks of it. The command each session was
# started with is what the manager reads back as launch provenance: a test serves it from the
# home's real service socket (`FakePtyService`) by giving the fake a list that service answers
# from.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.sessions.protocol import SessionBackend


class Backend(SessionBackend):
    def __init__(self, provenance: list[dict[str, Any]] | None = None) -> None:
        self.commands: dict[str, str] = {}
        self.killed: list[str] = []
        self.launches = 0
        self.fail_launch = False
        self.fail_reap = False
        # meiva:note
        # Called inside `new_session`, after the session exists: a launch that other work
        # interleaves with.
        self.during_launch: Callable[[], None] | None = None
        # meiva:note
        # Called inside `kill_session`: a reap that other work interleaves with.
        self.during_kill: Callable[[str], None] | None = None
        self._provenance = provenance

    # meiva:note
    # A session that is already running `command`, as the service would list it.
    def hold(self, name: str, command: str) -> None:
        self.commands[name] = command
        self._publish()

    def _publish(self) -> None:
        if self._provenance is None:
            return
        self._provenance[:] = [
            {
                "name": name,
                "pid": 1,
                "create_time": 1.0,
                "starttime": None,
                "cmd": "bash",
                "cwd": "/",
                "started_at": 1.0,
                "generation": None,
                "initial_command": command,
            }
            for name, command in self.commands.items()
        ]

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [name for name in self.commands if name.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.commands

    def session_generation(self, name: str) -> str | None:
        return None

    def host_generation(self) -> str | None:
        return "host-a"

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str]:
        self.killed.append(name)
        if self.during_kill is not None:
            self.during_kill(name)
        if self.fail_reap:
            return False, "survived"
        self.commands.pop(name, None)
        self._publish()
        return True, "forced"

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        self.launches += 1
        if not self.fail_launch:
            self.hold(name, cmd)
        if self.during_launch is not None:
            self.during_launch()
        return not self.fail_launch
