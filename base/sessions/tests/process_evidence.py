import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil


def detached_to_known_reaper(
    child_pid: int,
    caller_pid: int,
    caller_sid: int,
    ancestor_births: set[tuple[int, float]],
) -> bool:
    child = psutil.Process(child_pid)
    parent = child.parent()
    if parent is None or parent.pid == caller_pid or os.getsid(child_pid) == caller_sid:
        return False
    return parent.pid == 1 or (parent.pid, parent.create_time()) in ancestor_births


def process_evidence(pid: int) -> dict[str, object]:
    result: dict[str, object] = {"pid": pid}
    try:
        proc = psutil.Process(pid)
        result.update(
            name=proc.name(),
            birth=proc.create_time(),
            ppid=proc.ppid(),
            status=proc.status(),
            pgid=os.getpgid(pid),
            sid=os.getsid(pid),
        )
    except (psutil.NoSuchProcess, ProcessLookupError):
        result["observation"] = "gone"
    except (psutil.AccessDenied, PermissionError):
        result["observation"] = "unreadable"
    return result


def detach_evidence(child_pid: int) -> dict[str, object]:
    current = psutil.Process()
    ancestors = current.parents()
    subreaper = ctypes.c_int(-1)
    if sys.platform == "linux":
        result = ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(subreaper), 0, 0, 0)
        if result != 0:
            subreaper.value = -1
    return {
        "caller": process_evidence(current.pid),
        "child": process_evidence(child_pid),
        "caller_subreaper": subreaper.value,
        "caller_ancestors": [process_evidence(parent.pid) for parent in ancestors[:16]],
    }


def no_new_direct_children(
    spawner: psutil.Process, before: set[psutil.Process]
) -> tuple[bool, object]:
    try:
        lingering = set(spawner.children()) - before
    except psutil.NoSuchProcess:
        return True, "spawner gone"
    return not lingering, [c.pid for c in lingering]


def no_zombie_children(spawner: psutil.Process, before: set[psutil.Process]) -> tuple[bool, object]:
    try:
        children = set(spawner.children(recursive=True)) - before
    except psutil.NoSuchProcess:
        return True, "spawner gone"
    zombies: list[int] = []
    for child in children:
        try:
            if child.status() == psutil.STATUS_ZOMBIE:
                zombies.append(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return not zombies, zombies


def _ignore_sigterm(_signum: int, _frame: object) -> None:
    return


def _run_child_mode() -> None:
    # meiva:note
    # Test child entrypoint invoked via sys.executable -I with fixed mode names.
    mode = sys.argv[1]
    if mode == "noop":
        return
    if mode == "sleep":
        time.sleep(float(sys.argv[2]))
        return
    if mode == "sigign_only":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(300)
        return
    if mode == "sigign_sleep":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(sys.argv[2]).write_text("", encoding="utf-8")
        time.sleep(300)
        return
    if mode == "spawn_sleep":
        child = subprocess.Popen(["/bin/sleep", "300"])
        sys.stderr.write(str(child.pid))
        sys.stderr.flush()
        time.sleep(300)
        return
    if mode == "late_child":
        ready = Path(sys.argv[2])
        late = Path(sys.argv[3])
        signal.signal(signal.SIGTERM, _ignore_sigterm)
        ready.write_text("ready", encoding="utf-8")
        time.sleep(0.5)
        proc = subprocess.Popen([sys.executable, "-I", __file__, "sigign_only"])  # noqa: S603
        late.write_text(str(proc.pid), encoding="utf-8")
        time.sleep(300)
        return
    if mode == "detached_child":
        ready = Path(sys.argv[2])
        detached = Path(sys.argv[3])
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-I", __file__, "sleep", "300"],
            start_new_session=True,
        )
        detached.write_text(str(proc.pid), encoding="utf-8")
        ready.write_text("ready", encoding="utf-8")
        time.sleep(300)
        return
    if mode == "read_stdin_byte":
        sys.stdin.buffer.read(1)
        return
    raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    _run_child_mode()
