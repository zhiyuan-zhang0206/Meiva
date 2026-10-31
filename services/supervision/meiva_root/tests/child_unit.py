import os
import pathlib
import signal
import subprocess
import sys
import time

from base.host.exit_codes import CODE_BEHIND_MINIMUM_EXIT_CODE


def _sleep() -> None:
    time.sleep(300)


def _noop() -> None:
    return


def _read_stdin_byte() -> None:
    sys.stdin.buffer.read(1)


def _hold_group_member(pgid: str, reap: str) -> None:
    # meiva:note
    # Spawn a fixed test child scenario in the target process group.
    member = subprocess.Popen(  # noqa: S603
        [sys.executable, str(pathlib.Path(__file__)), "sleep"],
        process_group=int(pgid),
    )
    sys.stdout.write(f"{member.pid}\n")
    sys.stdout.flush()
    path = pathlib.Path(reap)
    while not path.exists():
        time.sleep(0.01)
    member.wait()


def _write_environment(output: str, name: str) -> None:
    pathlib.Path(output).write_text(os.environ[name])
    time.sleep(60)


def _ignore_term(ready: str) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path(ready).touch()
    time.sleep(60)


def _read_config(output: str, value: str) -> None:
    temporary = pathlib.Path(output + ".tmp")
    temporary.write_text(pathlib.Path(value).read_text())
    temporary.replace(output)
    time.sleep(60)


def _spawn_child(child_file: str) -> None:
    # meiva:note
    # Spawn a fixed test child scenario in the same process group.
    child = subprocess.Popen([sys.executable, str(pathlib.Path(__file__)), "sleep"])  # noqa: S603
    pathlib.Path(child_file).write_text(str(child.pid))
    time.sleep(60)


def _wait_for_file(trigger: str) -> None:
    path = pathlib.Path(trigger)
    while not path.exists():
        time.sleep(0.01)


def _closes_slowly(ready: str, close_s: str) -> None:

    def close(*_: object) -> None:
        time.sleep(float(close_s))
        sys.exit(0)

    signal.signal(signal.SIGTERM, close)
    pathlib.Path(ready).touch()
    time.sleep(60)


def _restart_down(ready: str, release: str) -> None:
    if pathlib.Path(release).exists():
        time.sleep(60)
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pathlib.Path(ready).touch()
        while not pathlib.Path(release).exists():
            time.sleep(0.05)
        sys.exit(0)


def _forked_late_child(child_file: str, ready: str) -> None:

    def finish(*_: object) -> None:
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
            os._exit(0)
        pathlib.Path(child_file).write_text(str(pid))
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    pathlib.Path(ready).touch()
    time.sleep(60)


def _fail_loudly(reason: str) -> None:
    # meiva:note
    # A wrapper that refuses the launch says why on stderr and nowhere else: the retirement
    # warning is only useful if it repeats those words.
    sys.stderr.write(f"{reason}\n")
    sys.stderr.flush()
    raise SystemExit(CODE_BEHIND_MINIMUM_EXIT_CODE)


def _code_gate_exit(marker: str) -> None:
    path = pathlib.Path(marker)
    path.write_text(path.read_text() + "start\n" if path.exists() else "start\n")
    raise SystemExit(CODE_BEHIND_MINIMUM_EXIT_CODE)


_SCENARIOS = {
    "sleep": _sleep,
    "noop": _noop,
    "read_stdin_byte": _read_stdin_byte,
    "hold_group_member": _hold_group_member,
    "write_environment": _write_environment,
    "ignore_term": _ignore_term,
    "read_config": _read_config,
    "spawn_child": _spawn_child,
    "wait_for_file": _wait_for_file,
    "closes_slowly": _closes_slowly,
    "restart_down": _restart_down,
    "forked_late_child": _forked_late_child,
    "code_gate_exit": _code_gate_exit,
    "fail_loudly": _fail_loudly,
}

if __name__ == "__main__":
    _SCENARIOS[sys.argv[1]](*sys.argv[2:])
