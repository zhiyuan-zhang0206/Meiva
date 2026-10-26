# meiva:note
# One native PgBouncer stop owner, including retained shutdown attempts.
#
# PgBouncer escalates a second SIGINT or SIGTERM to immediate shutdown. The exact native
# birth is persisted before the first signal (`pooler/stop_intent.py`); retries only wait for
# its exit.

from __future__ import annotations

import signal
from dataclasses import dataclass
from pathlib import Path

from base.clock import Timers
from base.cluster import port_preflight
from base.host.private_storage import write_private_bytes
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane.pooler.ini import listen_port
from cli.commands.data_plane.pooler.stop_intent import (
    NativeBirth,
    intent_names,
    native_birth,
    render_intent,
)
from cli.commands.data_plane.stop_budget import poll_delay, time_left

# meiva:note
# How often a wait for the pooler to exit observes it again.
_POLL_INTERVAL_S = 0.05

# meiva:note
# How long a forced SIGKILL is given to take effect.
_KILL_SETTLE_S = 0.5


def _birth(identity: OwnedProcess) -> NativeBirth:
    return native_birth(identity.pid, identity.birth, identity.starttime)


@dataclass(frozen=True)
class OwnedPooler:
    identity: OwnedProcess
    port: int
    config: Path
    timers: Timers

    @classmethod
    def from_config(cls, identity: OwnedProcess, config: Path, timers: Timers) -> OwnedPooler:
        return cls(identity, listen_port(config.read_text()), config, timers)

    def _stop_requested(self) -> bool:
        try:
            text = self.config.with_name("stop-intent.json").read_text()
        except FileNotFoundError:
            return False
        return intent_names(text, _birth(self.identity))

    # meiva:note
    # Preparation cannot rewrite or reload an already-stopping native birth.
    def require_accepting(self) -> None:
        port_preflight.require_listener(self.identity, self.port)
        if self._stop_requested():
            raise RuntimeError("PgBouncer stop was already requested; custody retained")

    # meiva:note
    # This birth's validated listeners, or None once it has exited.
    #
    # A retained drain finishes on its own, and a busy host's socket scan is slow enough for the
    # birth to exit while its listeners are scanned: a gone birth leaves nothing to stop. A live
    # one beside a foreign listener still refuses.
    def _listeners(self) -> frozenset[int] | None:
        try:
            return port_preflight.require_listener(self.identity, self.port, required=False)
        except RuntimeError:
            if self.identity.live():
                raise
            return None

    # meiva:note
    # Record the intent for this birth, then signal it once.
    #
    # `OwnedProcess.send_signal` returns False when the birth is already gone, or when its pid now
    # belongs to a different birth: the process this stop owns no longer exists, which is the
    # state the stop asks for, so nothing is signalled and the wait that follows returns at once.
    # A stranger on the recycled pid is never signalled, and a pid that changes birth between the
    # check and the delivery raises inside `send_signal`. The "native birth changed before signal"
    # error this stop used to raise covered the gone case too, which is now a finished stop.
    def _request_stop(self, deadline: float) -> None:
        budget = time_left(deadline, self.timers.monotonic())
        if budget is None:
            raise TimeoutError("PgBouncer stop deadline expired; custody retained")
        with file_lock(self.config.with_name("stop-intent.lock"), timeout_s=budget):
            if not self.identity.live():
                return
            listeners = self._listeners()
            if listeners is None:
                return
            requested = self._stop_requested()
            # meiva:note
            # Record before signalling, including when a previous native caller already closed
            # the listeners. Ambiguous delivery never authorizes a retry.
            if not requested:
                write_private_bytes(
                    self.config.with_name("stop-intent.json"),
                    render_intent(_birth(self.identity)).encode(),
                )
            if listeners and not requested:
                if time_left(deadline, self.timers.monotonic()) is None:
                    raise TimeoutError("PgBouncer stop deadline expired; custody retained")
                self.identity.send_signal(signal.SIGINT)

    def _wait(self, deadline: float) -> bool:
        while self.identity.live():
            delay = poll_delay(time_left(deadline, self.timers.monotonic()), _POLL_INTERVAL_S)
            if delay is None:
                return False
            self.timers.sleep(delay)
        return True

    # meiva:note
    # Request safe shutdown once; explicit force has a separate settle bound. The forced SIGKILL
    # follows the same rule as the first signal: a birth that is already gone is not signalled and
    # the settle wait ends at once.
    def stop(self, *, deadline: float, force: bool = False) -> bool:
        if not self.identity.live():
            return True
        try:
            self._request_stop(deadline)
        except (TimeoutError, LockTimeoutError):
            if not force:
                raise
        else:
            if self._wait(deadline):
                return True
        if not force:
            return False
        if not self.identity.live() or self._listeners() is None:
            return True
        self.identity.send_signal(signal.SIGKILL)
        return self._wait(self.timers.monotonic() + _KILL_SETTLE_S)
