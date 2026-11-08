# meiva:note
# A real root-control socket whose answers a lifecycle test scripts.
#
# The lifecycle drivers dial `<root run dir>/meiva-root.sock` through the real `RootClient`.
# This listener sits where the real root would: a unix socket under the test's home that
# speaks the K1 wire (`base.native_process.root_control.ipc`), keeps a unit table the verbs
# change, and names this test process as the root so the client's exact-peer check passes. With a
# `launch_digest` and a `runtime` it reports the whole birth evidence a real root reports, so
# `start_serving.observe_root` reads it unpatched: a start can publish serving against it.
# Use it to test the shell that talks to the root; a test of what to *do* about a root
# status asserts the pure plan instead and needs no listener.

from __future__ import annotations

import socketserver
import threading
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psutil

from base.clock.tests.seeded_rng import seeded_rng
from base.deploy.lifecycle import start_serving
from base.host.runtime_interpreter import LoadedRuntimeIdentity
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import (
    ErrorCode,
    ProtocolError,
    encode,
    error_response,
    ok_response,
    parse_request,
)
from base.paths import root_run_dir

__all__ = ["ScriptedRoot", "UnitRow", "scripted_root"]

_SOCKET_NAME = "meiva-root.sock"


# meiva:note
# One unit of the scripted tree; `state` is what the verbs change.
@dataclass
class UnitRow:
    unit_id: str
    state: str = "running"
    intent: str = "running"
    attach: str | None = None
    stop_tier: int | None = None
    manifest_digest: str | None = None
    last_error: str | None = None
    restart_limit_reached: bool = False
    # meiva:note
    # A root answers `down` only once the unit has exited: the reply waits this long.
    exit_delay_s: float = 0.0
    # meiva:note
    # The root refuses to bring this unit up (an internal error reply).
    refuse_up: bool = False
    # meiva:note
    # The root accepts `up`, but the unit exits again at once: it stays stopped.
    stays_down: bool = False
    # meiva:note
    # Another `meiva start` begins at the moment the root receives this unit's `up`: the race a
    # start that is still launching loses its serving generation to.
    begins_rival_start: bool = False
    # meiva:note
    # A file the launching unit writes at the moment the root receives its `up`: the source
    # changing under a start after the root was born.
    writes_on_up: Path | None = None

    def wire(self) -> dict[str, object]:
        row: dict[str, object] = {
            "id": self.unit_id,
            "state": self.state,
            "intent": self.intent,
            "last_error": self.last_error,
        }
        if self.attach is not None:
            row["attach"] = self.attach
        if self.stop_tier is not None:
            row["stop_tier"] = self.stop_tier
        if self.manifest_digest is not None:
            row["manifest_digest"] = self.manifest_digest
        return row


# meiva:note
# The scripted tree: its units, the launch digest it reports and every verb it served.
@dataclass
class ScriptedRoot:
    home: Path
    units: dict[str, UnitRow] = field(default_factory=dict[str, UnitRow])
    launch_digest: str | None = None
    # meiva:note
    # The loaded-source identity this root reports as its own; a real root captures it at launch.
    runtime: LoadedRuntimeIdentity | None = None
    calls: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])
    # meiva:note
    # The caller block every state verb carried, in the order the root read them: what a real root
    # records beside each state it serves.
    callers: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    # meiva:note
    # The wall-clock time each entry of `calls` was received, at the same index.
    asked_at: list[datetime] = field(default_factory=list[datetime])
    # meiva:note
    # Every `<verb> <unit>` request and `exited <unit>` completion, with the root's clock.
    events: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    # meiva:note
    # Raw wire rows answered in place of the unit table: a root whose status passes the client's
    # check but not the lifecycle commands' own reading of it.
    raw_unit_rows: list[dict[str, object]] | None = None
    # meiva:note
    # Answers to `resource` requests, by operation name: what a root returns for a registered
    # resource verb such as `admit-inputs`. An operation absent here is refused as unknown.
    resources: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def status(self) -> dict[str, object]:
        me = OwnedProcess.capture(psutil.Process())
        root: dict[str, object] = {
            "home": str(self.home.resolve()),
            "running": True,
            "pid": me.pid,
            "create_time": me.birth,
            "starttime": me.starttime,
        }
        if self.launch_digest is not None:
            root["launch_digest"] = self.launch_digest
        if self.runtime is not None:
            root["runtime"] = self.runtime.model_dump(mode="json")
        return {
            "root": root,
            "units": (
                [row.wire() for row in self.units.values()]
                if self.raw_unit_rows is None
                else self.raw_unit_rows
            ),
            "health": {
                row.unit_id: {"restart_limit_reached": row.restart_limit_reached}
                for row in self.units.values()
            },
        }

    def serve(self, verb: str, name: str | None) -> dict[str, object]:
        with self._guard:
            self.calls.append((verb, name))
            self.asked_at.append(datetime.now(UTC))
            if verb == "status":
                return dict(ok_response(self.status()))
            if verb == "resource":
                scripted = self.resources.get(name or "")
                if scripted is None:
                    return dict(
                        error_response(ErrorCode.UNKNOWN_UNIT, f"unknown resource {name!r}")
                    )
                return dict(ok_response(dict(scripted)))
            row = self.units.get(name or "")
            if row is None:
                return dict(error_response(ErrorCode.UNKNOWN_UNIT, f"unknown unit {name!r}"))
            self.events.append((f"{verb} {row.unit_id}", time.monotonic()))
        if verb == "up":
            if row.begins_rival_start:
                # meiva:note
                # A rival start draws from a stream of its own: the invocation under test mints
                # its generation from `CommandRuntime.rng` (seeded at 0 by the harness), and the
                # rival only takes the unit away from it if the two generations differ.
                start_serving.begin_start(self.home, seeded_rng(1))
            if row.writes_on_up is not None:
                row.writes_on_up.parent.mkdir(parents=True, exist_ok=True)
                row.writes_on_up.write_text("written while the unit launched\n")
            if row.refuse_up:
                return dict(error_response(ErrorCode.INTERNAL, f"refused to bring up {name!r}"))
            with self._guard:
                row.intent = "running"
                row.state = "stopped" if row.stays_down else "running"
                state = row.state
        elif verb in ("down", "force-down"):
            time.sleep(row.exit_delay_s)
            with self._guard:
                row.state = row.intent = "stopped"
                self.events.append((f"exited {row.unit_id}", time.monotonic()))
                state = row.state
        else:
            with self._guard:
                state = row.state
        return dict(ok_response({"unit": row.unit_id, "state": state}))


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        tree = getattr(self.server, "tree", None)
        assert isinstance(tree, ScriptedRoot)
        raw = self.rfile.readline()
        try:
            request = parse_request(raw)
        except ProtocolError as exc:
            self.wfile.write(encode(error_response(ErrorCode.INVALID_REQUEST, str(exc))))
            return
        caller = request.get("caller")
        if isinstance(caller, dict):
            with tree._guard:
                tree.callers.append(dict(caller))
        self.wfile.write(encode(tree.serve(request["verb"], request.get("name"))))


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, tree: ScriptedRoot) -> None:
        self.tree = tree
        super().__init__(str(path), _Handler)


# meiva:note
# Serve `units` at the home's root control socket until the block ends.
@contextmanager
def scripted_root(
    home: Path,
    units: Iterable[UnitRow] = (),
    *,
    launch_digest: str | None = None,
    runtime: LoadedRuntimeIdentity | None = None,
) -> Generator[ScriptedRoot]:
    tree = ScriptedRoot(
        home=home,
        units={row.unit_id: row for row in units},
        launch_digest=launch_digest,
        runtime=runtime,
    )
    run_dir = root_run_dir(home)
    run_dir.mkdir(parents=True, exist_ok=True)
    server = _Server(run_dir / _SOCKET_NAME, tree)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tree
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
