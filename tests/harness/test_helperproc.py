from __future__ import annotations

import ast
import contextlib
import functools
import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Callable, Generator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.host.permissions_helper.client import PermissionsHelperClient
from base.native_process.kernel_identity import pid_starttime_ticks
from base.sessions import helperproc
from base.sessions.helper_chain_guard import parent_chain_intact
from base.sessions.record import SessionRecord


@contextlib.contextmanager
def _helper_socket_server(
    handler: Callable[[dict[str, object]], dict[str, object]],
) -> Generator[PermissionsHelperClient]:
    # meiva:note
    # Serve the permissions-helper JSON-line protocol over a real Unix domain socket so tests exercise PermissionsHelperClient.spawn_process end-to-end without subclassing the client.
    # meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
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


# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
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
        backend = helperproc.HelperProcSessionBackend(lambda: fake_client, SYSTEM_TIMERS)

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


def test_new_session_preserves_argv_and_non_login_shell(
    unit_home: Path,
) -> None:
    calls: list[object] = []

    def handle_spawn(req: dict[str, object]) -> dict[str, object]:
        calls.append(req["argv"])
        return {"ok": True, "result": {"pid": os.getpid(), "reused": False}}

    argv = [sys.executable, "-m", "services.agent_runner.agent_host.daemon"]
    with _helper_socket_server(handle_spawn) as fake_client:
        backend = helperproc.HelperProcSessionBackend(lambda: fake_client, SYSTEM_TIMERS)
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
        helperproc.HelperProcSessionBackend(lambda: fake_client, SYSTEM_TIMERS).new_session(
            "meiva-service", "echo nope", unit_home, env={}
        )


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
        lambda: PermissionsHelperClient("/unused.sock"), SYSTEM_TIMERS
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
            lambda: PermissionsHelperClient("/unused.sock"), SYSTEM_TIMERS
        ).kill_session("meiva-live", graceful=True, timeout=10.0) == (True, "graceful")
        assert child.wait(timeout=10) == -signal.SIGTERM
        assert not path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
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
            lambda: PermissionsHelperClient("/unused.sock"), SYSTEM_TIMERS
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
            lambda: PermissionsHelperClient("/unused.sock"), SYSTEM_TIMERS
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
) -> None:
    from base.config import get_field, set_field
    from base.sessions import backend as session_backend

    previous_enabled = get_field("permissions_helper_enabled")
    previous_spawn = get_field("permissions_helper_spawn")
    set_field("permissions_helper_enabled", enabled)
    set_field("permissions_helper_spawn", spawn)
    try:
        backend = session_backend.get_backend(timers=SYSTEM_TIMERS, macos=is_macos)
    finally:
        set_field("permissions_helper_enabled", previous_enabled)
        set_field("permissions_helper_spawn", previous_spawn)
    assert type(backend).__name__ == expected


def test_backend_route_fails_closed_when_settings_are_unreadable() -> None:
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
        backend = session_backend.get_backend(timers=SYSTEM_TIMERS, macos=True)
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


def _check_parent_chain(
    links: dict[str, int],
    *,
    marker: str | None,
) -> bool:
    # meiva:note
    # Run the parent-chain check in a child process with an explicit environment dict so tests do not mutate os.environ.
    env = dict(os.environ)
    if marker is None:
        env.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    else:
        env["MEIVA_PERMISSIONS_HELPER_PID"] = marker
    res = subprocess.run(  # noqa: S603
        [sys.executable, __file__, "parent-chain", json.dumps(links)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return res.stdout.strip() == "1"


def test_parent_chain_guard_allows_unmanaged_and_rejects_malformed_markers() -> None:
    links = {"self": 100}
    assert _check_parent_chain(links, marker=None)
    assert _check_parent_chain(links, marker="100")
    assert not _check_parent_chain(links, marker="not-an-int")
    assert not _check_parent_chain(links, marker="0")
    assert not _check_parent_chain(links, marker="-3")


def test_parent_chain_guard_rejects_a_helper_outside_the_chain() -> None:
    assert not _check_parent_chain({"self": 100, "100": 1}, marker="101")


# meiva:note
# A root supervisor between a unit and the permissions helper does not break the helper ancestry check; malformed markers, broken intermediate links and an unrelated helper are rejected.
# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
def test_parent_chain_guard_accepts_the_helper_above_an_intermediate() -> None:
    assert _check_parent_chain({"self": 101, "101": 100}, marker="100")


def test_parent_chain_guard_rejects_a_broken_intermediate_link() -> None:
    assert not _check_parent_chain({"self": 101}, marker="100")


def test_parent_chain_guard_stops_at_the_depth_bound() -> None:
    links: dict[str, int] = {"self": 7000}
    for step in range(40):
        links[str(7000 + step)] = 7001 + step

    assert _check_parent_chain(links, marker="7030")
    assert not _check_parent_chain(links, marker="7040")


# meiva:note
# The real ancestor-chain probe launches the current interpreter with a fixed repository probe script and test-owned paths, then checks the reported ancestry for an intact or unrelated helper.
# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
def _run_probe_chain(marker_dir: Path, *, marker_value: str | None = None) -> str:
    env = None
    if marker_value is not None:
        env = {**os.environ, "MEIVA_TEST_HELPER_MARKER_VALUE": marker_value}
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).parents[1] / "components" / "base" / "helper_chain_probe.py"),
            "helper",
            str(marker_dir / "marker"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def test_parent_chain_guard_on_a_real_process_chain(tmp_path: Path) -> None:
    assert _run_probe_chain(tmp_path) == "intact"


def test_parent_chain_guard_rejects_an_outside_pid_on_a_real_chain(tmp_path: Path) -> None:
    outsider = _spawn_sleep_child()
    try:
        assert _run_probe_chain(tmp_path, marker_value=str(outsider.pid)) == "broken"
    finally:
        outsider.kill()
        outsider.wait(timeout=10)


async def test_beat_runs_the_parent_chain_check_before_the_liveness_beat() -> None:
    from services.agent_runner.agent_host import host_beat

    calls: list[str] = []

    class BrokenChain(BaseException):
        pass

    class FakeLiveness:
        def beat(self) -> None:
            calls.append("beat")

    def broken_chain() -> None:
        calls.append("chain")
        raise BrokenChain

    # meiva:note
    # The beat reads no process identity itself: the host hands it the check that does,
    # and every tick runs that check before the liveness beat.
    with pytest.raises(BrokenChain):
        await host_beat.beat_forever(
            cast(Any, FakeLiveness()),
            cast(Any, object()),
            cast(Any, object()),
            "runner-a",
            cast(Any, object()),
            require_helper_chain=broken_chain,
            read_alert_seconds=lambda: 30.0,
        )

    assert calls == ["chain"]


def test_host_boot_checks_the_parent_chain_first() -> None:
    from services.agent_runner.agent_host import daemon

    assert "require_helper_parent_chain(" in inspect.getsource(daemon.main)


def _beat_construction(function: Callable[..., object]) -> ast.Call:
    # meiva:note
    # The `ownership_beat_completion(...)` call inside `function`'s body: the one place that
    # decides which chain check a running host's beat gets.
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    [call] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ownership_beat_completion"
    ]
    return call


def test_run_hands_the_real_parent_chain_check_to_the_beat() -> None:
    from services.agent_runner.agent_host import daemon

    keywords = {keyword.arg: keyword.value for keyword in _beat_construction(daemon.run).keywords}
    [check] = [value for name, value in keywords.items() if name == "require_helper_chain"]

    assert isinstance(check, ast.Call)
    assert isinstance(check.func, ast.Name)
    assert daemon.run.__globals__[check.func.id] is functools.partial
    [target] = check.args
    assert isinstance(target, ast.Name)
    # meiva:note
    # `run` resolves the bound function from the daemon's own globals: the object `main` also checks
    # with at boot, never a same-named no-op.
    assert daemon.run.__globals__[target.id] is daemon.require_helper_parent_chain


def test_broken_parent_chain_exits_host_with_software_error() -> None:
    # meiva:note
    # Execute `require_helper_parent_chain` in a real child process so `os._exit(70)` is observed via its actual exit status.
    res = subprocess.run(  # noqa: S603
        [sys.executable, __file__, "require-parent-chain"],
        env={**os.environ, "MEIVA_PERMISSIONS_HELPER_PID": "not-an-int"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 70


def _child_main() -> None:
    import time

    mode = sys.argv[1]
    if mode == "sleep":
        time.sleep(60)
    elif mode == "parent-chain":
        spec = json.loads(sys.argv[2])
        pid = os.getpid()
        links = {pid if k == "self" else int(k): int(v) for k, v in spec.items()}
        sys.stdout.write("1" if parent_chain_intact(pid, environ=os.environ, ppids=links) else "0")
    elif mode == "require-parent-chain":
        from services.agent_runner.agent_host import daemon

        daemon.require_helper_parent_chain(pid=os.getpid(), environ=os.environ)
    else:
        raise ValueError(f"unknown child mode {mode!r}")


if __name__ == "__main__":
    _child_main()
