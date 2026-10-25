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

from base.native_process.ownership import OwnedProcess
from base.sessions.tests.refuse_helper import refuse_connect
from base.sessions.tests.scripted_helper import ScriptedHelper, scripted_at
from services.desktop.permissions_helper import launchd_job as jobs
from services.desktop.permissions_helper import lifecycle
from services.desktop.permissions_helper.tests.support import stopped_helper_plist
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_identity_reply,
    stage_signing_identity,
)


def test_isolated_helper_build_never_replaces_installed_artifact(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
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

    env = lifecycle.BuildEnv(
        app_dir=installed,
        source_hash="source",
        runner=run,
    )
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


def test_isolated_helper_rejects_installed_destination_before_building(
    tmp_path: Path,
) -> None:
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="outside the installed"):
        lifecycle.build_and_sign(
            destination=tmp_path,
            env=lifecycle.BuildEnv(app_dir=tmp_path),
        )


def _stale_installed_helper(
    tmp_path: Path,
    events: list[str],
    *,
    dr: str = "adhoc-dr-new",
    loaded: bool = False,
    pids: Sequence[int] = (),
) -> tuple[Path, lifecycle.BuildEnv]:
    home = tmp_path / "home"
    installed = home / "helper"
    exe = installed / "MeivaPermissionsHelper.app" / "Contents" / "MacOS" / "MeivaPermissionsHelper"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"old helper")
    state = {"source_hash": "old", "dr": dr, "signed_at": "2026-09-29T00:00:00+00:00"}
    (installed / "build-state.json").write_text(json.dumps(state))
    agents = tmp_path / "agents"
    agents.mkdir()
    stage_signing_identity(installed)

    def scan(_exe: Path) -> Sequence[int]:
        events.append("scan")
        return pids

    def launchd_run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
        events.append("launchd")
        if loaded:
            return subprocess.CompletedProcess(argv, 0, b"pid = 321\n", b"")
        return subprocess.CompletedProcess(argv, 113, b"", b"Could not find service")

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
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

    retirement = jobs.RetirementEnv(
        home=home,
        agents_dir=agents,
        pids=scan,
        runner=launchd_run,
    )
    env = lifecycle.BuildEnv(
        app_dir=installed,
        home=home,
        source_hash="new",
        retirement=retirement,
        runner=run,
    )
    return exe, env


@pytest.mark.parametrize("blocker", ["loaded", "plist", "pid"])
def test_stale_installed_helper_is_never_replaced_while_in_use(
    tmp_path: Path, blocker: str
) -> None:
    events: list[str] = []
    exe, env = _stale_installed_helper(
        tmp_path,
        events,
        loaded=blocker == "loaded",
        pids=[4242] if blocker == "pid" else (),
    )
    state = (exe.parents[3] / "build-state.json").read_text()
    if blocker == "plist":
        assert env.retirement is not None
        jobs.helper_job_plist_path(
            env.retirement.home,
            agents_dir=env.retirement.agents_dir,
        ).write_bytes(b"retained definition")
    reason = {"loaded": "launchd still has", "plist": "still registered", "pid": "pid 4242 still"}
    with pytest.raises(RuntimeError, match=reason[blocker]) as refused:
        lifecycle.build_and_sign(env=env)
    assert "run `meiva stop` first" in str(refused.value)
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

    assert lifecycle.build_and_sign(env=env) == (exe.parents[2], True)
    assert exe.read_bytes() == b"new helper"
    assert " ".join(events) == "launchd scan swiftc launchd scan codesign read-dr"
    state = json.loads((exe.parents[3] / "build-state.json").read_text())
    assert (state["source_hash"], state["dr"]) == ("new", "adhoc-dr-new")
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
        if (identity := fake_identity_reply(argv, app_dir=env.app_dir)) is not None:
            return identity
        if argv[0] == "swiftc":
            return subprocess.CompletedProcess(argv, 1, b"", b"swiftc failed")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="swiftc failed"):
        lifecycle.build_and_sign(env=replace(env, runner=fail))
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
    env = jobs.RetirementEnv(home=tmp_path, agents_dir=tmp_path, loaded=False)
    jobs.require_retired_helper(tmp_path / "unused-executable", env=env)
    child = subprocess.Popen(
        [sys.executable, "-m", "services.desktop.permissions_helper.tests.support"]
    )
    try:
        with pytest.raises(RuntimeError, match=rf"pid (\d+, )*{child.pid}\b.* still runs"):
            jobs.require_retired_helper(Path(sys.executable), env=env)
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
    path = jobs.helper_job_plist_path(home, agents_dir=tmp_path)
    path.write_bytes(b"retained definition")

    def unknown(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        assert argv[:2] == ["launchctl", "print"]
        return subprocess.CompletedProcess(argv, 5, b"", b"native manager unavailable")

    env = jobs.RetirementEnv(platform="darwin", agents_dir=tmp_path, runner=unknown)
    with pytest.raises(RuntimeError, match="cannot inspect"):
        jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
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
        jobs.retirement_plist(path, home, home / "socket")


@pytest.mark.parametrize("conflict", [None, "reappeared", "changed", "live-socket", "wrong-seed"])
def test_retained_definition_after_completed_stop_requires_exact_absence(
    tmp_path: Path, conflict: str | None
) -> None:
    import plistlib

    with tempfile.TemporaryDirectory(prefix="meivah-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        (home / "run").mkdir()
        path = jobs.helper_job_plist_path(home, agents_dir=tmp_path)
        stopped_helper_plist(home, path)
        if conflict == "wrong-seed":
            data = plistlib.loads(path.read_bytes())
            data["EnvironmentVariables"]["MEIVA_PERMISSIONS_HELPER_ROOT_SEED"] = (
                "/foreign/seed.json"
            )
            path.write_bytes(plistlib.dumps(data))
        neighbor = tmp_path / "neighbor.plist"
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

        env = jobs.RetirementEnv(platform="darwin", agents_dir=tmp_path, runner=runner)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            sock = home / "run/permissions-helper.23456.sock"
            listener.bind(str(sock))
            if conflict == "live-socket":
                listener.listen()
            if conflict is None:
                jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
                jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
                assert not path.exists() and len(reads) == 5
            else:
                with pytest.raises(RuntimeError):
                    jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
                assert path.exists()
        assert neighbor.read_bytes() == b"neighbor"


def test_helper_socket_inspection_error_retains_definition(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    path = jobs.helper_job_plist_path(home, agents_dir=tmp_path)
    original = stopped_helper_plist(home, path)
    (home / "run").mkdir()
    (home / "run/permissions-helper.23456.sock").write_bytes(b"untrusted socket input")

    def absent(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(cmd), 113, b"", b"Could not find service")

    env = jobs.RetirementEnv(platform="darwin", agents_dir=tmp_path, runner=absent)
    with pytest.raises(RuntimeError, match="socket absence"):
        jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
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
            jobs.wait_retirement_owner(owner, time.monotonic() + 0.05)
        assert owner.live(), "normal helper shutdown wait must not signal its owner"
        jobs.force_retirement_owner(owner, time.monotonic() + 5)
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
            jobs.force_retirement_owner(original, time.monotonic() + 5)
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
    path = jobs.helper_job_plist_path(home, agents_dir=tmp_path)
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

    env = jobs.RetirementEnv(platform="darwin", agents_dir=tmp_path, runner=command)
    if fault is None:
        jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
        jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
        assert not path.exists() and marker.exists()
        assert sum(args[0] == "bootout" for args in commands) == 1
    else:
        with pytest.raises(RuntimeError):
            jobs.unregister_helper(home, helper_port=23456, connect=refuse_connect, env=env)
        assert path.read_bytes() == original
        assert all(args[0] == "print" for args in commands)


@pytest.mark.parametrize("fault", ["wrong-ack", "lost-ack", "relaunch", "missing-intent"])
def test_unproven_shutdown_never_boots_out_native_job(tmp_path: Path, fault: str) -> None:
    from base.host.permissions_helper import client

    home = tmp_path.resolve()
    path = jobs.helper_job_plist_path(home, agents_dir=tmp_path)
    original = stopped_helper_plist(home, path)
    owner = OwnedProcess(321, 100.0, None)
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
        return {
            "stopping": True,
            "pid": 999 if fault == "wrong-ack" else 321,
            "run_dir": str(run_dir),
        }

    def command(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        args = argv[1:]
        commands.append(args)
        assert args[0] == "print", "unknown shutdown must retain native job authority"
        state = "pid = 321\n"
        if requested[0]:
            state = (
                "pid = 999\n"
                if fault == "relaunch"
                else "state = not running\nlast exit code = 0\n"
            )
        return subprocess.CompletedProcess(argv, 0, state.encode(), b"")

    env = jobs.RetirementEnv(
        platform="darwin",
        agents_dir=tmp_path,
        owner=owner,
        wait_for_owner=False,
        runner=command,
    )
    with pytest.raises(RuntimeError):
        jobs.unregister_helper(
            home,
            helper_port=23456,
            connect=scripted_at(ScriptedHelper(shutdown_helper=shutdown)),
            env=env,
        )
    assert requested[0] and path.read_bytes() == original
    assert all(args[0] == "print" for args in commands)


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
    from base.host.permissions_helper import client

    home = tmp_path.resolve() / "home"
    agents = tmp_path / "agents"
    agents.mkdir()
    path = jobs.helper_job_plist_path(home, agents_dir=agents)
    stopped_helper_plist(home, path)
    neighbor = agents / "neighbor.plist"
    neighbor.write_bytes(b"neighbor authority")
    alive = [True]
    owner = OwnedProcess(321, 100.0, None)
    commands: list[list[str]] = []
    loaded = [True]

    def shutdown(run_dir: Path) -> client.HelperShutdownResult:
        assert run_dir == home / "run" / "meiva-root"
        marker = run_dir / "helper-stopped"
        marker.write_bytes(b"stopped\n")
        marker.chmod(0o600)
        alive[0] = False
        return {"stopping": True, "pid": owner.pid, "run_dir": str(run_dir)}

    connect = scripted_at(ScriptedHelper(shutdown_helper=shutdown))

    def command(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        args = argv[1:]
        commands.append(args)
        if args[0] == "bootout":
            assert not alive[0], "bootout must wait for the captured native helper's exit"
            loaded[0] = False
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if alive[0]:
            return subprocess.CompletedProcess(argv, 0, b"pid = 321\n", b"")
        if loaded[0]:
            return subprocess.CompletedProcess(
                argv, 0, b"state = not running\nlast exit code = 0\n" + waiting, b""
            )
        return subprocess.CompletedProcess(argv, 113, b"", b"Could not find service")

    env = jobs.RetirementEnv(
        platform="darwin",
        agents_dir=agents,
        owner=owner,
        wait_for_owner=False,
        runner=command,
    )
    jobs.unregister_helper(home, helper_port=23456, connect=connect, env=env)
    jobs.unregister_helper(home, helper_port=23456, connect=connect, env=env)
    jobs.unregister_helper(home, helper_port=23456, connect=connect, env=env)
    assert not path.exists()
    assert neighbor.read_bytes() == b"neighbor authority"
    expected = f"{jobs.helper_job_domain()}/{jobs.helper_job_label(home)}"
    assert all(args[1] == expected for args in commands)
    assert [args[0] for args in commands].count("bootout") == 1
    assert all(args[0] in ("print", "bootout") for args in commands)
