from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.lifecycle import home_lifecycle_locks
from base.host.proc import run_bounded
from base.native_process.code_version import CodeVersionError
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.os_platform import LockTimeoutError
from base.native_process.root_control.instance_lock import (
    acquire_instance_lock,
    release_instance_lock,
)
from base.paths import run_dir
from gateway.schedules import router
from scripts.host_ops import update_schedule_script as repair


# meiva:note
# Hold the agent host's instance lock the way a running host does.
def host_lock_held(home: Path) -> int:
    return acquire_instance_lock(run_dir(home), "agent_host.lock")


def test_a_running_agent_host_refuses_the_repair_before_the_database(tmp_path: Path) -> None:
    lock = host_lock_held(tmp_path)
    try:
        with (
            pytest.raises(RuntimeError, match="requires the agent host to be stopped"),
            repair.repair_guard(tmp_path, pid=os.getpid()),
        ):
            pass
    finally:
        release_instance_lock(lock)


def test_a_stopped_agent_host_leaves_its_lock_free_for_the_next_host(tmp_path: Path) -> None:
    repair.require_agent_host_stopped(tmp_path)
    # meiva:note
    # The check took the lock and released it at once: a host starting now can take it.
    release_instance_lock(host_lock_held(tmp_path))


def test_wrong_interpreter_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"own \.venv"):
        repair.check_runtime_admission(tmp_path, tmp_path / "wrong", operator_home=tmp_path, env={})


def test_wrong_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must match"):
        repair.check_runtime_admission(
            tmp_path / "different-home",
            Path(sys.prefix).parent,
            operator_home=tmp_path,
            env={},
        )


def test_lifecycle_lock_prevents_a_competing_edit(tmp_path: Path) -> None:
    with (
        home_lifecycle_locks.resource_lock(
            home=tmp_path, pid=os.getpid(), purpose="test:other-lifecycle"
        ),
        pytest.raises(LockTimeoutError, match="last holder"),
        repair.repair_guard(tmp_path, pid=os.getpid(), lock_timeout_s=0.01),
    ):
        pass


def _git(source: Path, *args: str) -> str:
    # meiva:note
    # Fixed Git operations only touch a test-owned repository; hooks and signing stay off.
    result = subprocess.run(  # noqa: S603
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


def _captured_image(source: Path, known: bool) -> tuple[ProcessDbGate, LoadedCommit]:
    source.mkdir()
    if known:
        _git(source, "init", "--initial-branch=main")
        _git(source, "commit", "--allow-empty", "-m", "loaded A")
    image = LoadedCommit.capture(source_root=source)
    gate = ProcessDbGate(version=repair.source_version(source), process="test")
    assert (image.sha is not None) is known
    if not known:
        _git(source, "init", "--initial-branch=main")
    _git(source, "commit", "--allow-empty", "-m", "later B")
    assert image.sha != _git(source, "rev-parse", "HEAD")
    return gate, image


def _assert_image_gate(gate: ProcessDbGate, image: LoadedCommit) -> None:
    if image.sha is None:
        with pytest.raises(CodeVersionError, match="loaded commit"):
            gate.application_name()
        return
    git_dir = image.source_root / ".git"
    parked = image.source_root.parent / "installed-git-parked"
    git_dir.rename(parked)
    try:
        with pytest.raises(CodeVersionError, match="failed"):
            gate.application_name()
    finally:
        parked.rename(git_dir)
    assert gate.application_name().endswith(":v1")
    assert gate.min_read_due()
    git_dir.rename(parked)
    try:
        assert gate.application_name().endswith(":v1")
        gate.observe_minimum(1)
        assert not gate.min_read_due()
    finally:
        parked.rename(git_dir)


@pytest.mark.parametrize("known", [True, False], ids=["captured", "unknown"])
def test_copied_adapter_binds_the_installed_source_image(tmp_path: Path, known: bool) -> None:
    source = tmp_path / "installed-source"
    gate, image = _captured_image(source, known)
    _assert_image_gate(gate, image)


def test_wrong_home_source_is_refused(tmp_path: Path) -> None:
    source = Path(sys.prefix).parent.resolve()
    (tmp_path / "start-intent.json").write_text(
        json.dumps({"home": str(tmp_path), "checkout": str(tmp_path / "another-source")})
    )
    with pytest.raises(RuntimeError, match="source checkout"):
        repair.check_runtime_admission(tmp_path, source, operator_home=tmp_path, env={})


@pytest.mark.parametrize("key", ["MEIVA_PROCESS_PROFILE", "MEIVA_LAUNCHER_PROFILE"])
@pytest.mark.parametrize("profile", ["agent", "untrusted-delivery"])
def test_launcher_delivery_cannot_be_transplanted(key: str, profile: str, tmp_path: Path) -> None:
    source = Path(sys.prefix).parent.resolve()
    with pytest.raises(RuntimeError, match="without a launcher"):
        repair.check_runtime_admission(tmp_path, source, operator_home=tmp_path, env={key: profile})


@pytest.mark.parametrize("enabled", [False, True])
def test_versioned_edit_preserves_fields_and_defers_session_work(
    enabled: bool, db_conn: psycopg.Connection, database: Database
) -> None:
    with database.pool() as pool:
        row = router.create_blocking(
            pool,
            router.ScheduleCreate(
                name="maintenance-edit",
                description="keep description",
                script="print(1)\n",
                command="python custom.py",
                enabled=enabled,
            ),
            machine="gateway",
        )
        sid = row[0]
        assert (
            repair.replace_script(pool, sid, "print(2)\n", repair.script_sha256(row[11]))
            == "updated"
        )
        after = router.fetch_full_blocking(pool, sid)
        assert after[:8] == row[:8]
        assert after[11] == "print(2)\n"
        assert (
            repair.replace_script(pool, sid, "print(2)\n", repair.script_sha256(row[11]))
            == "unchanged"
        )
    assert db_conn.execute(
        "SELECT script, command, note FROM schedule_versions WHERE schedule_id=%s ORDER BY id",
        (sid,),
    ).fetchall() == [
        ("print(1)\n", "python custom.py", "initial"),
        ("print(2)\n", "python custom.py", "edit"),
    ]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == (
        [(sid,)] if enabled else []
    )
    assert db_conn.execute(
        "SELECT desired_revision, launch_count FROM schedules WHERE id=%s", (sid,)
    ).fetchone() == (1, 0)


def test_stale_hash_and_syntax_errors_do_not_write(
    db_conn: psycopg.Connection, database: Database
) -> None:
    with database.pool() as pool:
        row = router.create_blocking(
            pool,
            router.ScheduleCreate(name="stale", script="pass\n"),
            machine="gateway",
        )
        with pytest.raises(RuntimeError, match="expected old script"):
            repair.replace_script(pool, row[0], "print(2)\n", "0" * 64)
        with pytest.raises(Exception, match="syntax error"):
            repair.replace_script(pool, row[0], "def broken(\n", repair.script_sha256(row[11]))
        assert router.fetch_full_blocking(pool, row[0]) == row
    assert db_conn.execute("SELECT note FROM schedule_versions").fetchall() == [("initial",)]


def test_cli_does_not_accept_state_mutations(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        repair.main(
            [
                "1",
                "--home",
                str(tmp_path / "home"),
                "--source",
                str(tmp_path / "source"),
                "--script-file",
                "x.py",
                "--expected-sha256",
                "0" * 64,
                "--enable",
            ]
        )
    assert exc.value.code == 2


def test_utf8_digest_preserves_newlines() -> None:
    assert repair.script_sha256("pass\r\n") != repair.script_sha256("pass\n")


# meiva:note
# The external maintenance script uses the selected installation's interpreter and real writer while preserving schedule enabled/stopped state; it stores the new body without executing it.
# meiva:link scripts/host_ops/tests/docs/schedule-maintenance.meiva.okf.md
def test_external_file_uses_installed_runtime_and_real_writer(
    db_conn: psycopg.Connection,
    database: Database,
    db_url: str,
    tmp_path: Path,
) -> None:
    source = Path(os.environ.get("MEIVA_TEST_INSTALLED_SOURCE", Path(sys.prefix).parent)).resolve()
    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    (home / "start-intent.json").write_text(
        json.dumps({"home": str(home), "checkout": str(source)})
    )
    (home / ".env").write_text(
        f"MEIVA_DB_URL={db_url}\nMEIVA_SERVE_GATEWAY=true\n"
        "MEIVA_SERVE_AGENT_RUNNER=false\nMEIVA_MACHINE_NAME=external-script-test\n"
    )
    with database.pool() as pool:
        row = router.create_blocking(
            pool,
            router.ScheduleCreate(name="external", script="pass\n", enabled=True),
            machine="gateway",
        )
    external = tmp_path / "operator-tool.py"
    external.write_bytes(Path(repair.__file__).read_bytes())
    script_file = tmp_path / "prepared.py"
    script_file.write_bytes(b"raise AssertionError('the body must never run')\r\n")
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    child_env["MEIVA_HOME"] = str(home)
    result = run_bounded(
        [
            str(source / ".venv" / "bin" / "python"),
            str(external),
            str(row[0]),
            "--home",
            str(home),
            "--source",
            str(source),
            "--script-file",
            str(script_file),
            "--expected-sha256",
            repair.script_sha256(row[11]),
        ],
        timeout=30,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=child_env,
    )
    assert result.returncode == 0, result.stderr
    assert "updated script_sha256=" in result.stdout
    assert db_conn.execute(
        "SELECT enabled,status,script FROM schedules WHERE id=%s", (row[0],)
    ).fetchone() == (True, "stopped", script_file.read_bytes().decode())
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id=%s ORDER BY id", (row[0],)
    ).fetchall() == [("initial",), ("edit",)]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [
        (row[0],)
    ]


def test_main_preserves_unknown_failure_traceback(tmp_path: Path) -> None:
    script_file = tmp_path / "prepared.py"
    script_file.write_text("pass\n")
    with pytest.raises(RuntimeError, match=r"own \.venv"):
        repair.main(
            [
                "1",
                "--home",
                str(tmp_path),
                "--source",
                str(tmp_path),
                "--script-file",
                str(script_file),
                "--expected-sha256",
                "0" * 64,
            ]
        )
