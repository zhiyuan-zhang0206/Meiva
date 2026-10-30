from __future__ import annotations

import json
import socket
import threading
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil

from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import (
    ErrorCode,
    ProtocolError,
    Verb,
    encode,
    error_response,
    ok_response,
    parse_request,
)
from base.paths import root_run_dir, run_dir

LAUNCH_DIGEST = "b" * 64
RUNTIME_DIGEST = "a" * 64

_ACCEPT_TIMEOUT_S = 0.2
_JOIN_TIMEOUT_S = 5.0
# meiva:note
# An AF_UNIX address is 108 bytes including the terminating NUL, so 107 bytes of path. This is a
# kernel limit, not a tuning constant: a longer path fails at bind with an OSError that names no
# cause, which is why `_refuse_long_socket_path` reports the arithmetic first.
_UNIX_PATH_BYTES = 108


# meiva:note
# A loaded source identity has the shape `observe_root` validates: absolute paths and a digest.
def _runtime(home: Path) -> dict[str, Any]:
    return {
        "kind": "source",
        "code_root": str(home / "runtime" / "code"),
        "interpreter": str(home / "runtime" / "python"),
        "prefix": str(home / "runtime" / "venv"),
        "cwd": str(home / "runtime"),
        "source_digest": RUNTIME_DIGEST,
    }


# meiva:note
# The marked birth a `status` answer carries, in the receipt's `birth` shape: the receipt and the
# answer are the same fields by construction, not by a copy that can drift.
def birth_from_response(root: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "home": root["home"],
        "process": {field: root[field] for field in ("pid", "create_time", "starttime")},
        "launch_digest": root["launch_digest"],
        "runtime": root["runtime"],
    }


def receipt_path(home: Path) -> Path:
    return run_dir(home) / "start-serving.json"


# meiva:note
# Publish `birth` as this home's serving marker through a staged rename, and return the generation
# the marker carries.
def write_receipt(home: Path, birth: Mapping[str, Any], *, generation: str | None = None) -> str:
    published = generation or str(uuid.uuid4())
    state = {
        "schema_version": 2,
        "state": "serving",
        "generation": published,
        "birth": dict(birth),
    }
    path = receipt_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    staged.write_text(json.dumps(state), encoding="utf-8")
    staged.replace(path)
    return published


def clear_receipt(home: Path) -> None:
    receipt_path(home).unlink(missing_ok=True)


# meiva:note
# Keep `home`'s receipt in step with `gate`: the file publishes it, its absence clears it. This is
# the suite's toggle, not a second gate -- `born_identity` still confirms the marked birth against
# the socket on every read.
@contextmanager
def publish_with(
    gate: Path, *, home: Path, answer: Mapping[str, Any], poll_s: float = 0.05
) -> Generator[None]:
    birth = birth_from_response(answer["root"])
    stop = threading.Event()

    def follow_once() -> None:
        if gate.is_file():
            if not receipt_path(home).is_file():
                write_receipt(home, birth)
        else:
            clear_receipt(home)

    def follow() -> None:
        while not stop.wait(poll_s):
            follow_once()

    # meiva:note
    # The first pass runs before the caller's module starts: a child that already sees the gate
    # file must find the receipt published, not wait out a poll interval.
    follow_once()
    thread = threading.Thread(target=follow, name="serving-gate", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=_JOIN_TIMEOUT_S)
        clear_receipt(home)


def _answer(connection: socket.socket, answer: dict[str, Any]) -> None:
    with connection:
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                return
            raw += chunk
        try:
            request = parse_request(raw)
        except ProtocolError as exc:
            connection.sendall(encode(error_response(ErrorCode.INVALID_REQUEST, str(exc))))
            return
        if request["verb"] != Verb.STATUS:
            connection.sendall(
                encode(error_response(ErrorCode.UNKNOWN_VERB, "this socket answers status only"))
            )
            return
        connection.sendall(encode(ok_response(answer)))


# meiva:note
# Check the socket address against the kernel's limit before binding.
#
# `observe_root` pins the socket at `<home>/run/meiva-root/meiva-root.sock`, so a long home (a
# worktree, a per-test scratch dir) can exceed it. Failing here names the byte count and the fix;
# a bare `bind` error would not.
def _refuse_long_socket_path(socket_path: Path) -> None:
    size = len(str(socket_path))
    if size < _UNIX_PATH_BYTES:
        return
    raise RuntimeError(
        f"the fake root socket path is {size} bytes; an AF_UNIX address holds "
        f"{_UNIX_PATH_BYTES} including the terminating NUL. `observe_root` pins the socket at "
        f"<home>/run/meiva-root/meiva-root.sock, so point the home at a short directory "
        f"(for example /tmp/m-<8 hex>) instead of a deep worktree or per-test path: {socket_path}"
    )


# meiva:note
# A previous run that was killed outright leaves its socket file behind, and a path that nothing
# listens on cannot be bound again. Only an unreachable path is removed: a live server is left.
def _drop_stale_socket(socket_path: Path) -> None:
    if not socket_path.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(socket_path))
    except OSError:
        socket_path.unlink(missing_ok=True)
    else:
        raise RuntimeError(f"this home already has a root: another server answers at {socket_path}")
    finally:
        probe.close()


# meiva:note
# The answer this fake root serves for `home`, built without binding anything: `serve` publishes it
# on the socket, and a case that needs only a receipt writes that receipt from here.
def answer_for(
    *, home: Path, report_pid: int | None = None, running: bool = True, omit_root: bool = False
) -> dict[str, Any]:
    owner = OwnedProcess.capture(psutil.Process())
    root: dict[str, Any] = {
        "pid": owner.pid if report_pid is None else report_pid,
        "create_time": owner.birth,
        "starttime": owner.starttime,
        "home": str(home.resolve(strict=True)),
        "launch_digest": LAUNCH_DIGEST,
        "runtime": _runtime(home),
        "running": running,
    }
    return {"units": []} if omit_root else {"root": root, "units": []}


# meiva:note
# Answer `status` on `home`'s control socket with this process's own native birth, so the kernel's
# peer check passes for a server that runs inside the test's process; each departure reproduces
# one refusal `observe_root` makes. `report_pid` names another process (the kernel-reported peer
# is the only authenticating fact), `running=False` says the root is not running, and `omit_root`
# leaves the birth row out of an otherwise valid answer.
@contextmanager
def serve(
    *,
    home: Path,
    report_pid: int | None = None,
    running: bool = True,
    omit_root: bool = False,
) -> Generator[dict[str, Any]]:
    answer = answer_for(home=home, report_pid=report_pid, running=running, omit_root=omit_root)
    socket_path = root_run_dir(home) / "meiva-root.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_long_socket_path(socket_path)
    _drop_stale_socket(socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(8)
    listener.settimeout(_ACCEPT_TIMEOUT_S)
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            _answer(connection, answer)

    thread = threading.Thread(target=accept, name="status-root")
    thread.start()
    try:
        yield answer
    finally:
        stop.set()
        thread.join(timeout=_JOIN_TIMEOUT_S)
        listener.close()
        socket_path.unlink(missing_ok=True)
