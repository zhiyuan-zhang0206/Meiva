from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS, Timers
from base.host.permissions_helper.client import HelperAtSocket, SessionProc
from base.native_process.ownership import OwnedProcess
from base.sessions.tests.refuse_helper import refuse_connect
from base.sessions.tests.scripted_helper import ScriptedHelper, scripted_at
from services.desktop.permissions_helper import launchd_job as jobs
from services.desktop.permissions_helper import lifecycle
from services.desktop.permissions_helper.tests.support import (
    helper_build_env,
    launchctl_reply,
    launchd_facts,
    refuse_tools,
    stopped_helper_plist,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_identity_reply,
    stage_signing_identity,
)


def _absent_job(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(list(cmd), 113, b"", b"Could not find service")


# meiva:note
# The replacement guard over a home whose launchd job is absent: the processes that run the
# executable are the real ones.
class _RetirementGuard:
    def __init__(self, home: Path) -> None:
        self._home = home

    def require(self, executable: Path) -> None:
        jobs.require_retired_helper(
            executable,
            home=self._home,
            facts=launchd_facts(self._home),
            run=_absent_job,
            timers=SYSTEM_TIMERS,
        )


# meiva:note
# Retire `home`'s helper as the test process: the real user id, this process as the caller, and the
# real clock (the owner tests wait on real processes).
def _unregister(
    home: Path,
    *,
    connect: HelperAtSocket,
    run: jobs.BoundedRunner,
    timers: Timers = SYSTEM_TIMERS,
    force: bool = False,
) -> None:
    jobs.unregister_helper(
        home,
        helper_port=23456,
        connect=connect,
        facts=launchd_facts(home),
        caller_pid=os.getpid(),
        run=run,
        timers=timers,
        force=force,
    )


# meiva:note
# A scripted helper that also reports its (empty) process sessions, as the retirement proof asks.
class _RetiringHelper(ScriptedHelper):
    def session_list(self, prefix: str = "") -> list[SessionProc]:
        return self._answer("session_list", prefix)


def test_isolated_helper_build_never_replaces_installed_artifact(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    installed = home / "helper"
    installed.mkdir(parents=True)
    sentinel = installed / "build-state.json"
    sentinel.write_text("unchanged installed state")
    candidate = tmp_path / "candidate"
    stage_signing_identity(installed)

    commands: list[list[str]] = []

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
        if (identity := fake_identity_reply(argv, app_dir=installed)) is not None:
            return identity
        commands.append(argv)
        if argv[0] == "swiftc":
            Path(argv[-1]).write_bytes(b"test executable")
        if argv[:3] == ["codesign", "-d", "-r-"]:
            return subprocess.CompletedProcess(argv, 0, b"designated => adhoc-dr\n", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    env = helper_build_env(home, run)
    app, rebuilt = lifecycle.build_and_sign(destination=candidate, env=env)
    assert rebuilt and app == candidate / "MeivaPermissionsHelper.app"
    assert sentinel.read_text() == "unchanged installed state"
    assert sorted(path.name for path in installed.iterdir()) == [
        "build-state.json",
        "signing.keychain-db",
        "signing.keychain-pass",
    ]
    assert (candidate / "build-state.json").is_file()
    assert [Path(command[0]).name for command in commands] == ["swiftc", "codesign", "codesign"]


# meiva:note
# The account's own production helper directory is protected from an isolated build by the user
# home the facts carry, not by whichever home this process happens to run under.
def test_the_helper_directory_of_the_account_is_protected_from_isolated_builds(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user"
    app_dir = tmp_path / "home" / "helper"

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="outside the installed"):
        lifecycle.build_directory(
            user_home / ".meiva" / "helper", app_dir=app_dir, user_home=user_home
        )
    other = tmp_path / "somebody" / ".meiva" / "helper"
    assert lifecycle.build_directory(other, app_dir=app_dir, user_home=user_home) == other


def test_isolated_helper_rejects_installed_destination_before_building(
    tmp_path: Path,
) -> None:
    env = helper_build_env(tmp_path / "home", refuse_tools)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="outside the installed"):
        lifecycle.build_and_sign(destination=env.app_dir, env=env)


# meiva:note
# A stale helper installed under `<tmp>/home`, with launchd answering `loaded` for its job. Every
# tool call goes through one runner, which records the order of the steps in `events`.
def _stale_installed_helper(
    tmp_path: Path,
    events: list[str],
    *,
    dr: str = "adhoc-dr-new",
    loaded: bool = False,
) -> tuple[Path, lifecycle.BuildEnv]:
    home = tmp_path / "home"
    installed = home / "helper"
    exe = installed / "MeivaPermissionsHelper.app" / "Contents" / "MacOS" / "MeivaPermissionsHelper"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"old helper")
    state = {"source_hash": "old", "dr": dr, "signed_at": "2026-09-29T00:00:00+00:00"}
    (installed / "build-state.json").write_text(json.dumps(state))
    stage_signing_identity(installed)

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
        if (job := launchctl_reply(argv, loaded=loaded)) is not None:
            events.append("launchd")
            return job
        if (identity := fake_identity_reply(argv, app_dir=installed)) is not None:
            return identity
        if argv[:3] == ["codesign", "-d", "-r-"]:
            events.append("read-dr")
            return subprocess.CompletedProcess(argv, 0, b"designated => adhoc-dr-new\n", b"")
        if argv[:2] == ["codesign", "--verify"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        events.append(Path(argv[0]).name)
        if argv[0] == "swiftc":
            assert exe.read_bytes() == b"old helper", "the compile must precede removal"
            Path(argv[-1]).write_bytes(b"new helper")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    return exe, helper_build_env(home, run)


@pytest.mark.parametrize("blocker", ["loaded", "plist", "pid"])
def test_stale_installed_helper_is_never_replaced_while_in_use(
    tmp_path: Path, blocker: str
) -> None:
    events: list[str] = []
    exe, env = _stale_installed_helper(tmp_path, events, loaded=blocker == "loaded")
    state = (exe.parents[3] / "build-state.json").read_text()
    if blocker == "plist":
        jobs.helper_job_plist_path(env.home, facts=env.facts).parent.mkdir(parents=True)
        jobs.helper_job_plist_path(env.home, facts=env.facts).write_bytes(b"retained definition")
    if blocker == "pid":
        # meiva:note
        # A live process runs the installed executable: this test process, through a link.
        exe.unlink()
        exe.symlink_to(Path(sys.executable).resolve())
    reason = {
        "loaded": "launchd still has",
        "plist": "still registered",
        "pid": r"pid \d+(, \d+)* still runs",
    }
    with pytest.raises(RuntimeError, match=reason[blocker]) as refused:
        lifecycle.build_and_sign(destination=None, env=env)
    assert "run `meiva stop` first" in str(refused.value)
    if blocker != "pid":
        assert exe.read_bytes() == b"old helper"
    assert (exe.parents[3] / "build-state.json").read_text() == state
    assert not {"swiftc", "codesign"} & set(events)


@pytest.mark.parametrize("previous_dr", ["adhoc-dr-new", "adhoc-dr-old"])
def test_retired_stale_installed_helper_is_replaced_after_the_compile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    previous_dr: str,
) -> None:
    events: list[str] = []
    exe, env = _stale_installed_helper(tmp_path, events, dr=previous_dr)

    assert lifecycle.build_and_sign(destination=None, env=env) == (exe.parents[2], True)
    assert exe.read_bytes() == b"new helper"
    assert " ".join(events) == "launchd swiftc launchd codesign read-dr"
    state = json.loads((exe.parents[3] / "build-state.json").read_text())
    assert state["dr"] == "adhoc-dr-new"
    assert state["source_hash"] == lifecycle.source_content_hash(env.sources)
    warned = "need granting again" in capsys.readouterr().err
    assert warned == (previous_dr != "adhoc-dr-new")


def test_stale_installed_helper_survives_a_host_that_cannot_compile(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    exe, env = _stale_installed_helper(tmp_path, events)
    state = (exe.parents[3] / "build-state.json").read_text()

    def fail(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
        if (job := launchctl_reply(argv, loaded=False)) is not None:
            return job
        if (identity := fake_identity_reply(argv, app_dir=env.app_dir)) is not None:
            return identity
        if argv[0] == "swiftc":
            return subprocess.CompletedProcess(argv, 1, b"", b"swiftc failed")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="swiftc failed"):
        lifecycle.build_and_sign(destination=None, env=replace(env, run=fail))
    assert exe.read_bytes() == b"old helper"
    assert (exe.parents[3] / "build-state.json").read_text() == state
    assert "codesign" not in events


def test_isolated_artifact_stays_immutable_without_consulting_the_home_job(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    _, env = _stale_installed_helper(tmp_path, events)
    candidate = tmp_path / "candidate"
    exe = candidate / "MeivaPermissionsHelper.app" / "Contents" / "MacOS" / "MeivaPermissionsHelper"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"prepared helper")

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="immutable"):
        lifecycle.build_and_sign(destination=candidate, env=env)
    assert exe.read_bytes() == b"prepared helper"
    assert events == []


def test_replacement_guard_names_a_live_process_running_the_artifact(
    tmp_path: Path,
) -> None:
    guard = _RetirementGuard(tmp_path)
    guard.require(tmp_path / "unused-executable")
    child = subprocess.Popen(
        [sys.executable, "-m", "services.desktop.permissions_helper.tests.support"]
    )
    try:
        with pytest.raises(RuntimeError, match=rf"pid (\d+, )*{child.pid}\b.* still runs"):
            guard.require(Path(sys.executable))
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None,
    reason="native Swift filesystem intent contract",
)
def test_helper_stop_intent_survives_new_reader_and_refuses_corruption(tmp_path: Path) -> None:
    source = lifecycle.SOURCE.read_text()
    implementation = source.split("enum StopOwner", 1)[1].split("func helperRunDir", 1)[0]
    program = tmp_path / "intent.swift"
    program.write_text(
        "import Foundation\nimport Darwin\nenum OpError: Error { case bad(String) }\n"
        + "enum StopOwner"
        + implementation
        + "\nlet directory = CommandLine.arguments[1]\n"
        + "try StopIntent.store(directory, owner: .root)\n"
        + "let retained = try StopIntent.exists(directory, owner: .root); assert(retained)\n"
        + "try StopIntent.store(directory, owner: .root)\n"
        + "try StopIntent.clear(directory, owner: .root)\n"
        + "let cleared = try StopIntent.exists(directory, owner: .root); assert(!cleared)\n"
        + 'try "corrupt".write(toFile: StopIntent.path(directory, owner: .root), atomically: true, encoding: .utf8)\n'
        + 'do { _ = try StopIntent.exists(directory, owner: .root); fatalError("corrupt intent admitted") } catch {}\n'
    )
    result = subprocess.run(
        ["swift", str(program), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_helper_retirement_unknown_job_never_removes_definition(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    facts = launchd_facts(home)
    path = jobs.helper_job_plist_path(home, facts=facts)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"retained definition")

    def unknown(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        assert argv[:2] == ["launchctl", "print"]
        return subprocess.CompletedProcess(argv, 5, b"", b"native manager unavailable")

    with pytest.raises(RuntimeError, match="cannot inspect"):
        _unregister(home, connect=refuse_connect, run=unknown)
    assert path.read_bytes() == b"retained definition"


def test_helper_retirement_rejects_caller_ancestry_before_wire_or_signal(tmp_path: Path) -> None:
    import os

    import psutil

    with pytest.raises(RuntimeError, match="ancestor"):
        jobs.retirement_owner(
            f"pid = {os.getpid()}\n",
            tmp_path / "no-socket",
            psutil.Process().exe(),
            connect=refuse_connect,
            caller_pid=os.getpid(),
        )


# meiva:note
# The ancestry check asks about the caller it is given, not about the current process: the job's
# process is this one, the caller is another process outside its tree, so the check passes and the
# retirement goes on to dial the helper.
def test_helper_retirement_asks_about_the_caller_it_was_given(tmp_path: Path) -> None:
    import psutil

    with pytest.raises(AssertionError, match="dialled the permissions helper"):
        jobs.retirement_owner(
            f"pid = {os.getpid()}\n",
            tmp_path / "no-socket",
            psutil.Process().exe(),
            connect=refuse_connect,
            caller_pid=2**22,
        )


def test_helper_retirement_refuses_foreign_home_plist(tmp_path: Path) -> None:
    import plistlib

    home = tmp_path.resolve()
    path = tmp_path / "job.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": jobs.helper_job_label(home),
                "ProgramArguments": ["/test/helper"],
                "EnvironmentVariables": {"MEIVA_PERMISSIONS_HELPER_SOCKET": "/foreign/home/socket"},
            }
        )
    )
    with pytest.raises(RuntimeError, match="exact home"):
        jobs.retirement_plist(path, home, home / "socket", uid=os.getuid())


@pytest.mark.parametrize("conflict", [None, "reappeared", "changed", "live-socket", "wrong-seed"])
def test_retained_definition_after_completed_stop_requires_exact_absence(
    tmp_path: Path, conflict: str | None
) -> None:
    import plistlib

    with tempfile.TemporaryDirectory(prefix="meivah-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        (home / "run").mkdir()
        facts = launchd_facts(home)
        path = jobs.helper_job_plist_path(home, facts=facts)
        path.parent.mkdir(parents=True)
        stopped_helper_plist(home, path)
        if conflict == "wrong-seed":
            data = plistlib.loads(path.read_bytes())
            data["EnvironmentVariables"]["MEIVA_PERMISSIONS_HELPER_ROOT_SEED"] = (
                "/foreign/seed.json"
            )
            path.write_bytes(plistlib.dumps(data))
        neighbor = path.parent / "neighbor.plist"
        neighbor.write_bytes(b"neighbor")
        reads: list[str] = []

        def runner(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            argv = list(cmd)
            assert argv[:2] == ["launchctl", "print"]
            target = argv[2]
            assert target.endswith(jobs.helper_job_label(home))
            reads.append(target)
            if len(reads) == 3:
                if conflict == "reappeared":
                    return subprocess.CompletedProcess(argv, 0, b"pid = 321\n", b"")
                if conflict == "changed":
                    path.write_bytes(b"changed authority")
            return subprocess.CompletedProcess(argv, 113, b"", b"Could not find service")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            sock = home / "run/permissions-helper.23456.sock"
            listener.bind(str(sock))
            if conflict == "live-socket":
                listener.listen()
            if conflict is None:
                _unregister(home, connect=refuse_connect, run=runner)
                _unregister(home, connect=refuse_connect, run=runner)
                assert not path.exists() and len(reads) == 5
            else:
                with pytest.raises(RuntimeError):
                    _unregister(home, connect=refuse_connect, run=runner)
                assert path.exists()
        assert neighbor.read_bytes() == b"neighbor"


def test_helper_socket_inspection_error_retains_definition(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    path = jobs.helper_job_plist_path(home, facts=launchd_facts(home))
    path.parent.mkdir(parents=True)
    original = stopped_helper_plist(home, path)
    (home / "run").mkdir()
    (home / "run/permissions-helper.23456.sock").write_bytes(b"untrusted socket input")

    def absent(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(cmd), 113, b"", b"Could not find service")

    with pytest.raises(RuntimeError, match="socket absence"):
        _unregister(home, connect=refuse_connect, run=absent)
    assert path.read_bytes() == original


def test_helper_native_shutdown_wait_never_signals_unresponsive_owner(tmp_path: Path) -> None:
    import psutil

    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "services.desktop.permissions_helper.tests.support",
            str(ready),
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        owner = OwnedProcess.capture(psutil.Process(child.pid))
        with pytest.raises(TimeoutError, match="did not complete"):
            jobs.wait_retirement_owner(owner, time.monotonic() + 0.05, timers=SYSTEM_TIMERS)
        assert owner.live(), "normal helper shutdown wait must not signal its owner"
        jobs.force_retirement_owner(owner, time.monotonic() + 5, timers=SYSTEM_TIMERS)
        assert not owner.live()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_helper_force_signal_refuses_reused_native_birth() -> None:
    child = subprocess.Popen(
        [sys.executable, "-m", "services.desktop.permissions_helper.tests.support"]
    )
    try:
        original = OwnedProcess(child.pid, 0.0, None)
        with pytest.raises(RuntimeError, match="identity changed"):
            jobs.force_retirement_owner(original, time.monotonic() + 5, timers=SYSTEM_TIMERS)
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("fault", [None, "no-intent", "corrupt-intent", "running", "failed-exit"])
def test_idle_shutdown_retry_requires_native_success_and_retained_intent(
    tmp_path: Path, fault: str | None
) -> None:
    home = tmp_path.resolve()
    path = jobs.helper_job_plist_path(home, facts=launchd_facts(home))
    path.parent.mkdir(parents=True)
    original = stopped_helper_plist(home, path)
    marker = home / "run" / "meiva-root" / "helper-stopped"
    marker.parent.mkdir(parents=True)
    if fault != "no-intent":
        marker.write_bytes(b"corrupt" if fault == "corrupt-intent" else b"stopped\n")
        marker.chmod(0o600)
    commands: list[list[str]] = []
    loaded = [True]

    def command(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        args = argv[1:]
        commands.append(args)
        if args[0] == "bootout":
            loaded[0] = False
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if not loaded[0]:
            return subprocess.CompletedProcess(argv, 113, b"", b"Could not find service")
        state = "spawn scheduled" if fault == "running" else "not running"
        code = 1 if fault == "failed-exit" else 0
        return subprocess.CompletedProcess(
            argv, 0, f"state = {state}\nlast exit code = {code}\n".encode(), b""
        )

    if fault is None:
        _unregister(home, connect=refuse_connect, run=command)
        _unregister(home, connect=refuse_connect, run=command)
        assert not path.exists() and marker.exists()
        assert sum(args[0] == "bootout" for args in commands) == 1
    else:
        with pytest.raises(RuntimeError):
            _unregister(home, connect=refuse_connect, run=command)
        assert path.read_bytes() == original
        assert all(args[0] == "print" for args in commands)


# meiva:note
# The helper's own answers for a native process `pid`: it answers the ping as that process, speaks
# the stop protocol, owns no root and no sessions, and acknowledges (or not) a shutdown request.
def _helper_of(pid: int, shutdown: object) -> ScriptedHelper:
    return _RetiringHelper(
        ping={"pong": True, "pid": pid, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
        root_status={"state": "unseeded", "seeded": False},
        session_list=[],
        shutdown_helper=shutdown,
    )


@pytest.mark.parametrize("fault", ["wrong-ack", "lost-ack", "relaunch", "missing-intent"])
def test_unproven_shutdown_never_boots_out_native_job(tmp_path: Path, fault: str) -> None:
    from base.host.permissions_helper import client

    home = tmp_path.resolve()
    path = jobs.helper_job_plist_path(home, facts=launchd_facts(home))
    path.parent.mkdir(parents=True)
    child = subprocess.Popen(
        [sys.executable, "-m", "services.desktop.permissions_helper.tests.support"]
    )
    original = stopped_helper_plist(home, path, executable=str(Path(sys.executable).resolve()))
    requested = [False]
    commands: list[list[str]] = []

    def shutdown(run_dir: Path) -> client.HelperShutdownResult:
        requested[0] = True
        if fault != "missing-intent":
            marker = run_dir / "helper-stopped"
            marker.write_bytes(b"stopped\n")
            marker.chmod(0o600)
        if fault == "lost-ack":
            raise RuntimeError("lost response after durable intent")
        if fault in ("relaunch", "missing-intent"):
            child.kill()
        return {
            "stopping": True,
            "pid": 999 if fault == "wrong-ack" else child.pid,
            "run_dir": str(run_dir),
        }

    def command(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        args = argv[1:]
        commands.append(args)
        assert args[0] == "print", "unknown shutdown must retain native job authority"
        state = f"pid = {child.pid}\n"
        if requested[0]:
            state = (
                "pid = 999\n"
                if fault == "relaunch"
                else "state = not running\nlast exit code = 0\n"
            )
        return subprocess.CompletedProcess(argv, 0, state.encode(), b"")

    try:
        with pytest.raises(RuntimeError):
            _unregister(home, connect=scripted_at(_helper_of(child.pid, shutdown)), run=command)
        assert requested[0] and path.read_bytes() == original
        assert all(args[0] == "print" for args in commands)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


_REGION_GRANT_GUARD = (
    "    guard CGPreflightScreenCaptureAccess() else {\n"
    '        throw OpError.bad("Screen Recording grant missing for region capture")\n'
    "    }\n"
)


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="native serial helper request and Foundation child contract",
)
def test_helper_shutdown_waits_for_inflight_gui_child_before_acknowledging() -> None:
    with tempfile.TemporaryDirectory(prefix="meiva-hreq-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        run_dir = home / "run"
        run_dir.mkdir()
        sock = home / "helper.sock"
        ready, release = home / "child-ready", home / "child-release"
        script = home / "child.sh"
        script.write_text('touch "$1"\nwhile [ ! -e "$2" ]; do sleep 0.01; done\n')
        source = (
            lifecycle.SOURCE.read_text()
            .replace("    reportResponsiblePID()\n", "")
            .replace("    registerPermissions()\n", "")
            .replace(_REGION_GRANT_GUARD, "")
        )
        source = source.replace(
            'URL(fileURLWithPath: "/usr/sbin/screencapture")', 'URL(fileURLWithPath: "/bin/sh")'
        )
        source = source.replace(
            '["-x", "-R\\(x),\\(y),\\(w),\\(h)", path]',
            json.dumps([str(script), str(ready), str(release)]),
        )
        assert 'p.arguments = ["' + str(script) in source
        program, executable = home / "main.swift", home / "helper"
        program.write_text(source)
        compiled = subprocess.run(
            ["swiftc", str(program), "-o", str(executable)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert compiled.returncode == 0, compiled.stderr.decode()

        def request(method: str, **arguments: object) -> dict[str, object]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(str(sock))
                connection.sendall(
                    json.dumps({"id": 1, "method": method, **arguments}).encode() + b"\n"
                )
                return json.loads(connection.makefile("rb").readline())

        environment = dict(os.environ)
        environment.update(
            MEIVA_PERMISSIONS_HELPER_SOCKET=str(sock),
            MEIVA_PERMISSIONS_HELPER_ROOT_SEED=str(run_dir / "seed.json"),
        )
        process = subprocess.Popen(
            [str(executable)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            deadline = time.monotonic() + 10
            while not sock.exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            with ThreadPoolExecutor(max_workers=2) as pool:
                capture = pool.submit(
                    request,
                    "screencapture_region",
                    x=0,
                    y=0,
                    w=1,
                    h=1,
                    path=str(home / "image.png"),
                )
                while not ready.exists():
                    assert not capture.done() and time.monotonic() < deadline
                    time.sleep(0.01)
                shutdown = pool.submit(request, "helper_shutdown", run_dir=str(run_dir))
                time.sleep(0.1)
                assert not shutdown.done() and not (run_dir / "helper-stopped").exists()
                release.touch()
                assert capture.result(timeout=5)["ok"] is True
                assert shutdown.result(timeout=5)["ok"] is True
            assert process.wait(timeout=5) == 0
            assert (run_dir / "helper-stopped").read_bytes() == b"stopped\n"
            assert not sock.exists()
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("waiting", [b"", b"pid = 0\n"])
def test_exact_home_helper_retirement_keeps_neighbor_and_waits_for_native_exit(
    tmp_path: Path, waiting: bytes
) -> None:
    import psutil

    from base.host.permissions_helper import client

    home = tmp_path.resolve() / "home"
    facts = launchd_facts(home)
    path = jobs.helper_job_plist_path(home, facts=facts)
    path.parent.mkdir(parents=True)
    stopped_helper_plist(home, path, executable=str(Path(sys.executable).resolve()))
    neighbor = path.parent / "neighbor.plist"
    neighbor.write_bytes(b"neighbor authority")
    child = subprocess.Popen(
        [sys.executable, "-m", "services.desktop.permissions_helper.tests.support"]
    )
    try:
        owner = OwnedProcess.capture(psutil.Process(child.pid))
        commands: list[list[str]] = []
        loaded = [True]

        def shutdown(run_dir: Path) -> client.HelperShutdownResult:
            assert run_dir == home / "run" / "meiva-root"
            marker = run_dir / "helper-stopped"
            marker.write_bytes(b"stopped\n")
            marker.chmod(0o600)
            child.kill()
            return {"stopping": True, "pid": owner.pid, "run_dir": str(run_dir)}

        connect = scripted_at(_helper_of(child.pid, shutdown))

        def command(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            argv = list(cmd)
            args = argv[1:]
            commands.append(args)
            if args[0] == "bootout":
                assert not owner.live(), "bootout must wait for the captured native helper's exit"
                loaded[0] = False
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if owner.live():
                return subprocess.CompletedProcess(argv, 0, f"pid = {owner.pid}\n".encode(), b"")
            if loaded[0]:
                return subprocess.CompletedProcess(
                    argv, 0, b"state = not running\nlast exit code = 0\n" + waiting, b""
                )
            return subprocess.CompletedProcess(argv, 113, b"", b"Could not find service")

        _unregister(home, connect=connect, run=command)
        _unregister(home, connect=connect, run=command)
        _unregister(home, connect=connect, run=command)
        assert not path.exists()
        assert neighbor.read_bytes() == b"neighbor authority"
        expected = f"{facts.domain}/{jobs.helper_job_label(home)}"
        assert all(args[1] == expected for args in commands)
        assert [args[0] for args in commands].count("bootout") == 1
        assert all(args[0] in ("print", "bootout") for args in commands)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
