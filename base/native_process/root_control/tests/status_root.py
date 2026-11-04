from __future__ import annotations

import json
import socket
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

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
# A loaded source identity as the answer carries it.
class RuntimeFacts(TypedDict):
    kind: str
    code_root: str
    interpreter: str
    prefix: str
    cwd: str
    source_digest: str


# meiva:note
# The birth row of a `status` answer: the process, the home, the launch digest and the loaded runtime.
class RootFacts(TypedDict):
    pid: int
    create_time: float
    starttime: int | None
    home: str
    launch_digest: str
    runtime: RuntimeFacts
    running: bool


# meiva:note
# The process fields of a marked birth.
class ProcessFacts(TypedDict):
    pid: int
    create_time: float
    starttime: int | None


# meiva:note
# The marked birth a receipt carries.
class BirthFacts(TypedDict):
    home: str
    process: ProcessFacts
    launch_digest: str
    runtime: RuntimeFacts


# meiva:note
# What the fake root answers to `status`.
class StatusAnswer(TypedDict):
    root: RootFacts
    units: list[object]


# meiva:note
# An answer whose birth row is left out.
class RootlessAnswer(TypedDict):
    units: list[object]


# meiva:note
# A loaded source identity has the shape `observe_root` validates: absolute paths and a digest.
def _runtime(home: Path) -> RuntimeFacts:
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
def birth_from_response(root: RootFacts) -> BirthFacts:
    return {
        "home": root["home"],
        "process": {
            "pid": root["pid"],
            "create_time": root["create_time"],
            "starttime": root["starttime"],
        },
        "launch_digest": root["launch_digest"],
        "runtime": root["runtime"],
    }


def receipt_path(home: Path) -> Path:
    return run_dir(home) / "start-serving.json"


# meiva:note
# Publish `birth` as this home's serving marker through a staged rename, and return the generation
# the marker carries.
def write_receipt(home: Path, birth: BirthFacts, *, generation: str | None = None) -> str:
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
    gate: Path, *, home: Path, answer: StatusAnswer, poll_s: float = 0.05
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


def _answer(connection: socket.socket, answer: StatusAnswer | RootlessAnswer) -> None:
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
def answer_for(*, home: Path, report_pid: int | None = None, running: bool = True) -> StatusAnswer:
    owner = OwnedProcess.capture(psutil.Process())
    root: RootFacts = {
        "pid": owner.pid if report_pid is None else report_pid,
        "create_time": owner.birth,
        "starttime": owner.starttime,
        "home": str(home.resolve(strict=True)),
        "launch_digest": LAUNCH_DIGEST,
        "runtime": _runtime(home),
        "running": running,
    }
    return {"root": root, "units": []}


# meiva:note
# Answer `status` on `home`'s control socket with this process's own native birth, so the kernel's
# peer check passes for a server that runs inside the test's process; each departure reproduces
# one refusal `observe_root` makes. `report_pid` names another process (the kernel-reported peer
# is the only authenticating fact), and `running=False` says the root is not running.
@contextmanager
def serve(
    *, home: Path, report_pid: int | None = None, running: bool = True
) -> Generator[StatusAnswer]:
    answer = answer_for(home=home, report_pid=report_pid, running=running)
    with _serving(home, answer):
        yield answer


# meiva:note
# `serve` for an answer whose birth row is left out: the one refusal `observe_root` makes about a
# well-formed answer that names no root.
@contextmanager
def serve_without_root(*, home: Path) -> Generator[RootlessAnswer]:
    answer: RootlessAnswer = {"units": []}
    with _serving(home, answer):
        yield answer


@contextmanager
def _serving(home: Path, answer: StatusAnswer | RootlessAnswer) -> Generator[None]:
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
                connection = listener.accept()[0]
            except TimeoutError:
                continue
            _answer(connection, answer)

    thread = threading.Thread(target=accept, name="status-root")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=_JOIN_TIMEOUT_S)
        listener.close()
        socket_path.unlink(missing_ok=True)
