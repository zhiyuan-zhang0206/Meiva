# meiva:note
# The window's one connection to its hosts and the one log it keeps. Every command goes to a host
# through the operator's own `ssh`, found on the PATH the process was started with; every line
# the hosts print, and every line the window says, is redacted and written to both the console
# and a log only its owner can read. Nothing here imports application code, so a window runs from
# a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from subprocess import PIPE, STDOUT
from typing import Protocol

from .plan.host_commands import login_shell
from .plan.redaction import redact


# meiva:note
# A window step that cannot complete. The half that starts hosts and the half that verifies them
# raise and catch the same class.
class FailedError(RuntimeError):
    pass


# meiva:note
# A window that refused before touching anything. The caller exits 2 for it, as against 1 for a
# failure after the first effect.
class RefusedError(FailedError):
    pass


# meiva:note
# How often the window looks again while it waits: the roster until every host agrees, and the
# lease countdown while the operator releases takeovers. Five seconds is enough to watch the
# window move without burying the leases named above it.
@dataclass(frozen=True)
class Pacing:
    roster_poll_s: float = 5.0
    lease_tick_s: float = 5.0


# meiva:note
# The clock a window measures its waits with: the roster window is the only one, and it is spent as
# a started instant, a poll interval and a sleep between polls. This is the module's own port rather
# than `base.clock.Timers` because nothing here imports application code (a window runs from a bare
# operator checkout); the entry point wraps the process clock and a test hands in a counter.
class Timers(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class Session:
    def __init__(
        self,
        *,
        log_dir: Path,
        half: str,
        dry_run: bool,
        repo: Path,
        env: Mapping[str, str],
        now: datetime,
        pacing: Pacing,
        timers: Timers,
    ) -> None:
        self.dry_run = dry_run
        self.repo = repo
        self.env = env
        self.pacing = pacing
        self.now = now
        self.timers = timers
        self.stamp = f"{now:%Y%m%dT%H%M%S}"
        self.os: dict[str, str] = {}
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = log_dir / f"{now:%Y%m%dT%H%M%S.%fZ}-{half}.log"
        self.log = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.say(f"log: {path}")

    def say(self, line: str) -> None:
        line = redact(line)
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    # meiva:note
    # Run one command on a host, saying it first. A read (`effect=False`) always runs; an effect
    # is only said under --dry-run. The command goes through the host's login shell once its
    # operating system is known, so the host's profile puts its own tools on PATH.
    def run(self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None) -> str:
        self.say(f"[{alias}] {'$' if effect else 'read:'} " + script.replace("\n", "\n    "))
        if effect and self.dry_run:
            return ""
        if alias in self.os:
            script = login_shell(self.os[alias], script)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", alias, script]
        try:
            process = subprocess.Popen(
                argv,
                stdin=PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=PIPE,
                stderr=STDOUT,
                text=True,
                env=dict(self.env),
            )
        except OSError as exc:
            raise FailedError(f"{alias}: cannot run ssh: {exc}") from exc
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin)
            process.stdin.close()
        lines: list[str] = []
        for raw in process.stdout or ():
            line = raw.rstrip("\n")
            lines.append(line)
            self.say(f"    {alias}| {line}")
        if rc := process.wait():
            raise FailedError(f"{alias}: exit {rc}: {lines[-1] if lines else 'no output'}")
        return "\n".join(lines)
