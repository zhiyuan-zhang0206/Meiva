from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Generator
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.host.permissions_helper.client import PermissionsHelperClient
from base.native_process.kernel_identity import pid_starttime_ticks
from base.sessions import helperproc
from base.sessions.record import SessionRecord


@contextlib.contextmanager
def _helper_socket_server(
    handler: Callable[[dict[str, object]], dict[str, object]],
) -> Generator[PermissionsHelperClient]:
    # meiva:note
    # Serve the permissions-helper JSON-line protocol over a real Unix domain socket so tests exercise PermissionsHelperClient.spawn_process end-to-end without subclassing the client.
    # meiva:link tests/harness/docs/helper-process-tests.md
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="mhp-") as temp_dir:
        sock_path = Path(temp_dir) / "helper.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(8)
        server.settimeout(0.2)
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn:
                    buf = bytearray()
                    while not buf.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    if not buf:
                        continue
                    req = json.loads(buf.decode("utf-8"))
                    reply = handler(req)
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield PermissionsHelperClient(sock_path)
        finally:
            stop.set()
            server.close()
            thread.join(timeout=5)


def _current_process_record(*, generation: str | None = None) -> SessionRecord:
    process = psutil.Process()
    return SessionRecord(
        pid=process.pid,
        create_time=process.create_time(),
        cmd="test command",
        cwd="/test",
        started_at=123.0,
        starttime=pid_starttime_ticks(process.pid),
        generation=generation,
    )


# meiva:link tests/harness/docs/helper-process-tests.md
def test_new_session_preserves_login_shell_env_stderr_and_record(
    unit_home: Path,
) -> None:
    from base.sessions import env_forwarding

    calls: list[dict[str, object]] = []

    def handle_spawn(req: dict[str, object]) -> dict[str, object]:
        calls.append(
            {
                "name": req["name"],
                "argv": req["argv"],
                "env": req["env"],
                "cwd": req["cwd"],
                "stdout": req["stdout"],
                "stderr": req["stderr"],
            }
        )
        return {"ok": True, "result": {"pid": os.getpid(), "reused": False}}

    prefix = env_forwarding.venv_activation_prefix()
    stderr = unit_home / "logs" / "service.stderr.log"
    with _helper_socket_server(handle_spawn) as fake_client:
        backend = helperproc.HelperProcSessionBackend(lambda: fake_client, SYSTEM_TIMERS, unit_home)

        assert backend.new_session(
            "meiva-service",
            "python -m services.example.daemon",
            unit_home,
            env={"MEIVA_TEST": "forwarded"},
            stderr_append=stderr,
        )
        assert calls == [
            {
                "name": "meiva-service",
                "argv": [
                    "/bin/bash",
                    "-lc",
                    f"cd {unit_home.as_posix()} && {prefix}exec python -m services.example.daemon",
                ],
                "env": {"MEIVA_TEST": "forwarded"},
                "cwd": str(unit_home),
                "stdout": str(unit_home / "logs" / "meiva-service.out.log"),
                "stderr": str(stderr),
            }
        ]
        record = SessionRecord.read(unit_home / "run" / "sessions" / "meiva-service.json")
        assert record is not None
        assert record.pid == os.getpid()
        assert record.cmd == "python -m services.example.daemon"
        assert backend.has_session("meiva-service")

        assert backend.new_session(
            "meiva-service",
            "python -m services.example.daemon",
            unit_home,
            env={"MEIVA_TEST": "forwarded"},
        )
        assert len(calls) == 1


def test_the_records_and_transcripts_live_under_the_home_the_backend_was_given(
    unit_home: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-home"
    other.mkdir()

    def handle_spawn(_req: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "result": {"pid": os.getpid(), "reused": False}}

    with _helper_socket_server(handle_spawn) as fake_client:
        backend = helperproc.HelperProcSessionBackend(lambda: fake_client, SYSTEM_TIMERS, other)

        assert backend.new_session("meiva-service", "true", other, env={})

        assert SessionRecord.read(other / "run" / "sessions" / "meiva-service.json") is not None
        assert not (unit_home / "run" / "sessions" / "meiva-service.json").exists()
        assert backend.session_log_path("meiva-service") == other / "logs" / "meiva-service.out.log"


def test_new_session_preserves_argv_and_non_login_shell(
    unit_home: Path,
) -> None:
    calls: list[object] = []

    def handle_spawn(req: dict[str, object]) -> dict[str, object]:
        calls.append(req["argv"])
        return {"ok": True, "result": {"pid": os.getpid(), "reused": False}}

    argv = [sys.executable, "-m", "services.agent_runner.agent_host.daemon"]
    with _helper_socket_server(handle_spawn) as fake_client:
        backend = helperproc.HelperProcSessionBackend(lambda: fake_client, SYSTEM_TIMERS, unit_home)
        assert backend.new_session("meiva-agent", argv, unit_home, env={})
        assert backend.new_session(
            "meiva-shell-command",
            "echo direct",
            unit_home,
            env={},
            login_shell=False,
        )
    assert calls == [argv, ["/bin/sh", "-c", "echo direct"]]


def test_spawn_failure_is_loud_and_has_no_fallback(
    unit_home: Path,
) -> None:
    def refuse_spawn(_req: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "error": "socket refused"}

    with (
        _helper_socket_server(refuse_spawn) as fake_client,
        pytest.raises(RuntimeError, match=r"helper is down or refused.*meiva converge.*helper log"),
    ):
        helperproc.HelperProcSessionBackend(
            lambda: fake_client, SYSTEM_TIMERS, unit_home
        ).new_session("meiva-service", "echo nope", unit_home, env={})


def test_record_registry_drives_list_timestamps_and_generation(unit_home: Path) -> None:
    sessions = unit_home / "run" / "sessions"
    live = _current_process_record(generation="generation-1")
    dead = SessionRecord(
        pid=999_999_999,
        create_time=-1.0,
        cmd="dead",
        cwd="/test",
        started_at=456.0,
    )
    live.write(sessions / "meiva-live.json")
    dead.write(sessions / "meiva-dead.json")
    backend = helperproc.HelperProcSessionBackend(
        lambda: PermissionsHelperClient("/unused.sock"),
        SYSTEM_TIMERS,
        unit_home,
    )

    assert backend.list_sessions("meiva-") == ["meiva-live"]
    assert not (sessions / "meiva-dead.json").exists()
    assert backend.session_started_at("meiva-live") == 123.0
    assert backend.session_started_ats(["meiva-live", "missing"]) == {
        "meiva-live": 123.0,
        "missing": None,
    }
    assert backend.session_generation("meiva-live") == "generation-1"
    assert backend.session_log_path("meiva-live") == unit_home / "logs" / "meiva-live.out.log"


def _spawn_sleep_child() -> subprocess.Popen[bytes]:
    # meiva:note
    # Re-enter this test file in sleep mode so signal and parent-chain tests exercise real OS processes without `python -c`.
    return subprocess.Popen([sys.executable, __file__, "sleep"])  # noqa: S603


def test_kill_signals_record_pid_and_unlinks_only_after_confirmed_death(
    unit_home: Path,
) -> None:
    child = _spawn_sleep_child()
    try:
        record = replace(_current_process_record(), pid=child.pid)
        record = replace(
            record,
            create_time=psutil.Process(child.pid).create_time(),
            starttime=pid_starttime_ticks(child.pid),
        )
        path = unit_home / "run" / "sessions" / "meiva-live.json"
        record.write(path)

        assert helperproc.HelperProcSessionBackend(
            lambda: PermissionsHelperClient("/unused.sock"),
            SYSTEM_TIMERS,
            unit_home,
        ).kill_session("meiva-live", graceful=True, timeout=10.0) == (True, "graceful")
        assert child.wait(timeout=10) == -signal.SIGTERM
        assert not path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# meiva:link tests/harness/docs/helper-process-tests.md
def test_graceful_delivery_tolerates_whole_second_create_time_drift(
    unit_home: Path,
) -> None:
    child = _spawn_sleep_child()
    try:
        drifted = SessionRecord(
            pid=child.pid,
            create_time=psutil.Process(child.pid).create_time() - 1.0,
            cmd="test command",
            cwd="/test",
            started_at=123.0,
            starttime=None,
        )
        drifted.write(unit_home / "run" / "sessions" / "meiva-live.json")

        assert helperproc.HelperProcSessionBackend(
            lambda: PermissionsHelperClient("/unused.sock"),
            SYSTEM_TIMERS,
            unit_home,
        ).graceful_signal("meiva-live", expected=drifted)
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_graceful_delivery_refuses_a_birth_beyond_the_tolerance(
    unit_home: Path,
) -> None:
    child = _spawn_sleep_child()
    try:
        replaced = SessionRecord(
            pid=child.pid,
            create_time=psutil.Process(child.pid).create_time() + 60.0,
            cmd="test command",
            cwd="/test",
            started_at=123.0,
            starttime=None,
        )
        replaced.write(unit_home / "run" / "sessions" / "meiva-live.json")

        assert not helperproc.HelperProcSessionBackend(
            lambda: PermissionsHelperClient("/unused.sock"),
            SYSTEM_TIMERS,
            unit_home,
        ).graceful_signal("meiva-live", expected=replaced)
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.parametrize(
    ("is_macos", "enabled", "spawn", "expected"),
    [
        (False, True, True, "PosixProcSessionBackend"),
        (True, False, True, "PosixProcSessionBackend"),
        (True, True, False, "PosixProcSessionBackend"),
        (True, True, True, "HelperProcSessionBackend"),
    ],
)
def test_backend_route_matrix(
    is_macos: bool,
    enabled: bool,
    spawn: bool,
    expected: str,
    tmp_path: Path,
) -> None:
    from base.config import get_field, set_field
    from base.sessions import backend as session_backend

    previous_enabled = get_field("permissions_helper_enabled")
    previous_spawn = get_field("permissions_helper_spawn")
    set_field("permissions_helper_enabled", enabled)
    set_field("permissions_helper_spawn", spawn)
    try:
        backend = session_backend.get_backend(timers=SYSTEM_TIMERS, home=tmp_path, macos=is_macos)
    finally:
        set_field("permissions_helper_enabled", previous_enabled)
        set_field("permissions_helper_spawn", previous_spawn)
    assert type(backend).__name__ == expected


def test_backend_route_fails_closed_when_settings_are_unreadable(tmp_path: Path) -> None:
    from base.config import get_field, set_field
    from base.sessions import backend as session_backend

    # meiva:note
    # Raise on truthiness evaluation so helper_spawn_enabled hits its fail-closed exception path without patching base.config.settings.
    class _UnreadableFlag:
        def __bool__(self) -> bool:
            raise RuntimeError("unreadable")

    previous_enabled = get_field("permissions_helper_enabled")
    set_field("permissions_helper_enabled", _UnreadableFlag())
    try:
        backend = session_backend.get_backend(timers=SYSTEM_TIMERS, home=tmp_path, macos=True)
    finally:
        set_field("permissions_helper_enabled", previous_enabled)
    assert type(backend).__name__ == "PosixProcSessionBackend"


def test_permissions_helper_spawn_defaults_off() -> None:
    from base.config import FIELD_INFOS, field_alias
    from base.config.domains.services.settings import ServiceSettings

    assert ServiceSettings.model_fields["permissions_helper_spawn"].default is False
    assert field_alias("permissions_helper_spawn") == "MEIVA_PERMISSIONS_HELPER_SPAWN"
    assert FIELD_INFOS["permissions_helper_spawn"].json_schema_extra == {
        "capability": "agent-runner",
        "restart_required": "",
        "writable": False,
        "sensitive": False,
        "scope": "host",
        "remote_writable": True,
    }


def _child_main() -> None:
    import time

    mode = sys.argv[1]
    if mode == "sleep":
        time.sleep(60)
    else:
        raise ValueError(f"unknown child mode {mode!r}")


if __name__ == "__main__":
    _child_main()
