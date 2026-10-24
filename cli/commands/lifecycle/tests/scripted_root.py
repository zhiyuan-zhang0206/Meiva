# meiva:note
# A real root-control socket whose answers a lifecycle test scripts.
#
# The lifecycle drivers dial `<root run dir>/meiva-root.sock` through the real `RootClient`.
# This listener sits where the real root would: a unix socket under the test's home that
# speaks the K1 wire (`base.native_process.root_control.ipc`), keeps a unit table the verbs
# change, and names this test process as the root so the client's exact-peer check passes.
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

from base.deploy.lifecycle import start_serving
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

    def wire(self) -> dict[str, object]:
        row: dict[str, object] = {
            "id": self.unit_id,
            "state": self.state,
            "intent": self.intent,
            "last_error": self.last_error,
        }
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
    calls: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])
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

    def status(self) -> dict[str, object]:
        me = OwnedProcess.capture(psutil.Process())
        root: dict[str, object] = {
            "pid": me.pid,
            "create_time": me.birth,
            "starttime": me.starttime,
        }
        if self.launch_digest is not None:
            root["launch_digest"] = self.launch_digest
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
        self.calls.append((verb, name))
        self.asked_at.append(datetime.now(UTC))
        if verb == "status":
            return dict(ok_response(self.status()))
        row = self.units.get(name or "")
        if row is None:
            return dict(error_response(ErrorCode.UNKNOWN_UNIT, f"unknown unit {name!r}"))
        self.events.append((f"{verb} {row.unit_id}", time.monotonic()))
        if verb == "up":
            if row.begins_rival_start:
                start_serving.begin_start(self.home)
            if row.refuse_up:
                return dict(error_response(ErrorCode.INTERNAL, f"refused to bring up {name!r}"))
            row.intent = "running"
            row.state = "stopped" if row.stays_down else "running"
        elif verb in ("down", "force-down"):
            time.sleep(row.exit_delay_s)
            row.state = row.intent = "stopped"
            self.events.append((f"exited {row.unit_id}", time.monotonic()))
        return dict(ok_response({"unit": row.unit_id, "state": row.state}))


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
    home: Path, units: Iterable[UnitRow] = (), *, launch_digest: str | None = None
) -> Generator[ScriptedRoot]:
    tree = ScriptedRoot(
        home=home,
        units={row.unit_id: row for row in units},
        launch_digest=launch_digest,
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
