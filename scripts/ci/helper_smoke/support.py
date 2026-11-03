"""Shared primitives for the two-section chain smoke: failures, probes, evidence IO."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn

from base.clock import Timers
from base.host.permissions_helper.client import PermissionsHelperClient, RootStatus
from scripts.boundary.untyped import is_object_dict, is_object_list, object_of

# meiva:note
# A decoded JSON object from the processes the smoke drives as a black box (the root's control socket, the unit probes): the readers below narrow the fields it reads.
JsonObject = dict[str, object]

ROOT_SOCKET = "meiva-root.sock"
ROOT_WAIT_S = 30.0
_POLL_S = 0.25
UNIT_IDS = ("heartbeat", "heartbeat-b", "heartbeat-c")


UNIT_PROBE = '''\
\
"""Unit probe: side-effect-free preflight queries + heartbeat; answers sampled rounds.

TCCAccessPreflight never prompts and never touches protected data; tccd still
records every call with this process's attribution, which the smoke reads back
to prove the launchd -> helper -> root -> unit chain resolves to the helper.
"""

import ctypes
import json
import os
import sys
import time

SERVICES = [
    "kTCCServiceSystemPolicyDesktopFolder",
    "kTCCServiceSystemPolicyAllFiles",
    "kTCCServiceScreenCapture",
]

_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
_tcc.TCCAccessPreflight.restype = ctypes.c_int
_tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def preflight(service):
    cf_service = _cf.CFStringCreateWithCString(None, service.encode(), 0x08000100)
    return _tcc.TCCAccessPreflight(ctypes.c_void_p(cf_service), None)


def main():
    results_path, beat_path = sys.argv[1], sys.argv[2]
    request_path = results_path + ".req"
    rounds_path = results_path + ".rounds"
    results = {}
    for service in SERVICES:
        results[service] = preflight(service)
    with open(results_path, "w") as handle:
        json.dump({"pid": os.getpid(), "ppid": os.getppid(), "services": results}, handle)
    seen = None
    while True:
        with open(beat_path, "a") as handle:
            handle.write("%.0f\\n" % time.time())
        try:
            with open(request_path) as handle:
                current = handle.read().strip()
        except OSError:
            current = None
        if current and current != seen:
            seen = current
            record = {
                "round": current,
                "ts": round(time.time(), 3),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "pgid": os.getpgid(0),
                "sid": os.getsid(0),
                "services": {service: preflight(service) for service in SERVICES},
            }
            with open(rounds_path, "a") as handle:
                handle.write(json.dumps(record) + "\\n")
        time.sleep(1)


main()
'''


class SmokeError(RuntimeError):
    """One smoke phase failed; the message carries the phase and the detail."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"FAIL(phase={phase}): {detail}")
        self.phase = phase
        self.detail = detail


def fail(phase: str, detail: str) -> NoReturn:
    """Raise one phase failure (kept out of the try bodies for TRY301)."""
    raise SmokeError(phase, detail)


# meiva:note
# Fixed argv lists built in this file, no untrusted input.
def run(
    cmd: list[str],
    *,
    check: bool = True,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )
    if check and proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip()[-800:]
        fail("run", f"{cmd[0]} exited {proc.returncode}: {tail}")
    return proc


def wait_for[T](
    what: str, predicate: Callable[[], T | None], timeout: float, phase: str, *, timers: Timers
) -> T:
    deadline = timers.monotonic() + timeout
    while timers.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        timers.sleep(_POLL_S)
    fail(phase, f"timed out after {timeout:.0f}s waiting for {what}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ps_line(*pids: int) -> str:
    proc = run(["ps", "-o", "pid=,ppid=,lstart=,command=", "-p", ",".join(str(p) for p in pids)])
    return proc.stdout.strip()


def save(evidence: Path, name: str, text: str) -> None:
    (evidence / name).write_text(text + "\n")


def launchctl_domain(uid: int) -> str:
    return f"gui/{uid}"


def bootout(label: str, *, uid: int) -> None:
    run(["launchctl", "bootout", f"{launchctl_domain(uid)}/{label}"], check=False, timeout=30)


def job_pid(label: str) -> int | None:
    proc = run(["launchctl", "list"], check=False, timeout=30)
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            pid_field = parts[0].strip()
            return int(pid_field) if pid_field.isdigit() else None
    return None


def kill(pid: int, sig: int = signal.SIGTERM) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)


def tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text()[-limit:]
    except OSError:
        return "<missing>"


def refusal(call: Callable[[], object]) -> str:
    """The error text of a call that must be refused ("" when it was accepted)."""
    try:
        call()
    except Exception as exc:  # the refusal message is the assertion
        return str(exc)
    return ""


# A status/ping poll that cannot get an answer yet: socket absent or refusing (OSError),
# empty or malformed reply (ValueError), helper error reply (PermissionsHelperError and
# SmokeError are RuntimeErrors). Anything else is a bug in the poll and surfaces.
_NOT_ANSWERING = (OSError, ValueError, RuntimeError)


def ping_or_none(helper_client: PermissionsHelperClient) -> bool | None:
    try:
        return helper_client.ping()["pong"]
    except _NOT_ANSWERING:
        return None


# meiva:note
# `value` as a JSON object; anything else is a `TypeError` naming `what`.
def json_object(value: object, what: str) -> JsonObject:
    if not is_object_dict(value):
        raise TypeError(f"{what} is {type(value).__name__}, not an object")
    return value


# meiva:note
# `value` as a JSON array of objects; anything else is a `TypeError` naming `what`.
def json_objects(value: object, what: str) -> list[JsonObject]:
    if not is_object_list(value):
        raise TypeError(f"{what} is {type(value).__name__}, not an array")
    return [json_object(item, f"{what} item") for item in value]


# meiva:note
# `record[key]` as an int; a missing key or another type is a `TypeError` naming the key.
def whole_number(record: Mapping[str, object], key: str) -> int:
    value = record[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} is {type(value).__name__}, not an int")
    return value


# meiva:note
# The pid of the root a status document reports as running, else None.
def running_root_pid(status: JsonObject) -> int | None:
    root = json_object(status["root"], "status root")
    pid = root.get("pid")
    return pid if root.get("running") and isinstance(pid, int) else None


def status_if_running(
    root_status: Callable[[], JsonObject], *, excluding: int | None = None
) -> JsonObject | None:
    try:
        status = root_status()
    except _NOT_ANSWERING:
        return None
    pid = running_root_pid(status)
    if pid is None or (excluding is not None and pid == excluding):
        return None
    return status


def status_if_keeper_running(
    helper_root_status: Callable[[], RootStatus],
) -> RootStatus | None:
    try:
        status = helper_root_status()
    except _NOT_ANSWERING:
        return None
    if status["state"] == "running" and "pid" in status:
        return status
    return None


# meiva:note
# The pid of the foreign root a keeper in `conflict` reports, None when it reports none.
def conflict_pid_of(status: RootStatus) -> int | None:
    conflict = status.get("conflict")
    return None if conflict is None else conflict.get("pid")


# meiva:note
# The live root pid a running keeper reports.
def keeper_pid(status: RootStatus) -> int:
    pid = status.get("pid")
    if pid is None:
        fail("conflict", f"the keeper reports no pid for its root: {status}")
    return pid


def status_if_conflict(
    helper_root_status: Callable[[], RootStatus],
) -> RootStatus | None:
    try:
        status = helper_root_status()
    except _NOT_ANSWERING:
        return None
    return status if status["state"] == "conflict" else None


def relaunched_helper(label: str, old_pid: int):
    pid = job_pid(label)
    if pid is None or pid == old_pid:
        return None
    return pid


# meiva:note
# The live pid of unit `unit_id` in a root status document.
def live_unit_pid(status: JsonObject, unit_id: str) -> int:
    for entry in json_objects(status["units"], "status units"):
        pid = entry.get("pid")
        if entry["id"] == unit_id and isinstance(pid, int):
            return pid
    fail("chain", f"unit {unit_id!r} has no live pid in {json.dumps(status)[:400]}")


def ppid_of(pid: int) -> int:
    proc = run(["ps", "-o", "ppid=", "-p", str(pid)])
    return int(proc.stdout.strip())


def root_call(run_dir: Path, verb: str) -> JsonObject:
    """Send one K1 verb over the raw control socket (stdlib only).

    The smoke drives the root as a black box — process tree, socket, K1
    verbs — so it never depends on the root package's client API.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10.0)
        sock.connect(str(run_dir / ROOT_SOCKET))
        sock.sendall(json.dumps({"verb": verb}).encode() + b"\n")
        line = sock.makefile("rb").readline()
    response = json_object(object_of(json.loads, line), "root reply")
    if not response.get("ok"):
        fail("root", f"{verb} refused: {response}")
    result = response.get("result")
    if not is_object_dict(result):
        fail("root", f"{verb} returned non-object result: {response}")
    return result
