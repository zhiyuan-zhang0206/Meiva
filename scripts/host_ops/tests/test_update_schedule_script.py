from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from base.db import Database
from base.deploy.lifecycle import home_lifecycle_locks
from base.host.proc import run_bounded
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


def test_a_running_agent_host_refuses_the_repair_before_the_database(
    tmp_path: Path,
) -> None:
    def installed(_home: Path, _source: Path) -> None:
        pass

    def unexpected_database(**_kwargs: object) -> None:
        pytest.fail("a refused repair must not build a database handle")

    lock = host_lock_held(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="requires the agent host to be stopped"):
            repair.repair_script(
                home=tmp_path,
                source=tmp_path,
                schedule_id=1,
                script="pass\n",
                expected_sha256="0" * 64,
                load_runtime_fn=installed,
                capture_fn=lambda *, source_root: LoadedCommit(source_root=source_root, sha="x"),
                version_fn=lambda _img: lambda: 1,
                db_from_settings_fn=unexpected_database,
            )
    finally:
        release_instance_lock(lock)


def test_a_stopped_agent_host_leaves_its_lock_free_for_the_next_host(tmp_path: Path) -> None:
    repair.require_agent_host_stopped(tmp_path)
    # meiva:note
    # The check took the lock and released it at once: a host starting now can take it.
    release_instance_lock(host_lock_held(tmp_path))


def test_wrong_interpreter_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"own \.venv"):
        repair.load_installed_runtime(tmp_path, tmp_path / "wrong")


def test_wrong_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must match"):
        repair.load_installed_runtime(tmp_path / "different-home", Path(sys.prefix).parent)


def test_lifecycle_lock_prevents_a_competing_edit(tmp_path: Path) -> None:
    def installed(_home: Path, _source: Path) -> None:
        pass

    owner_lock = home_lifecycle_locks.resource_lock
    with (
        owner_lock(purpose="test:other-lifecycle"),
        pytest.raises(LockTimeoutError, match="last holder"),
    ):
        repair.repair_script(
            home=tmp_path,
            source=tmp_path,
            schedule_id=1,
            script="pass\n",
            expected_sha256="0" * 64,
            load_runtime_fn=installed,
            lock_fn=partial(owner_lock, timeout_s=0.01),
            capture_fn=lambda *, source_root: LoadedCommit(source_root=source_root, sha="x"),
            version_fn=lambda _img: lambda: 1,
        )


def test_copied_adapter_binds_the_installed_source_image(tmp_path: Path) -> None:
    source = tmp_path / "installed-source"
    image = LoadedCommit(source_root=source, sha="installed-code")
    captures: list[Path] = []
    borrowed_gates: list[object] = []
    db = MagicMock(spec=Database)

    def capture(*, source_root: Path) -> LoadedCommit:
        captures.append(source_root)
        return image

    def from_settings(*, gate: object) -> Database:
        borrowed_gates.append(gate)
        return db

    def installed(home: Path, installed_source: Path) -> None:
        assert home == tmp_path
        assert installed_source == source

    def version(loaded: LoadedCommit) -> Callable[[], int]:
        assert loaded is image
        return lambda: 37

    def replace_script(*_args: object) -> str:
        return "updated"

    assert (
        repair.repair_script(
            home=tmp_path,
            source=source,
            schedule_id=1,
            script="pass\n",
            expected_sha256="0" * 64,
            load_runtime_fn=installed,
            capture_fn=capture,
            version_fn=version,
            db_from_settings_fn=from_settings,
            replace_fn=replace_script,
        )
        == "updated"
    )
    assert captures == [source]
    assert len(borrowed_gates) == 1
    from base.db.code_version_gate import ProcessDbGate

    gate = borrowed_gates[0]
    assert isinstance(gate, ProcessDbGate)
    assert gate.application_name().endswith(":v37")
    assert gate.min_read_due()
    gate.observe_minimum(37)
    assert not gate.min_read_due()


def test_wrong_home_source_is_refused(tmp_path: Path) -> None:
    source = Path(sys.prefix).parent
    (tmp_path / "start-intent.json").write_text(
        json.dumps({"home": str(tmp_path), "checkout": str(tmp_path / "another-source")})
    )
    with pytest.raises(RuntimeError, match="source checkout"):
        repair.load_installed_runtime(tmp_path, source, env={"MEIVA_HOME": str(tmp_path)})


@pytest.mark.parametrize("key", ["MEIVA_PROCESS_PROFILE", "MEIVA_LAUNCHER_PROFILE"])
def test_launcher_delivery_cannot_be_transplanted(key: str, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="without a launcher"):
        repair.load_installed_runtime(
            tmp_path,
            Path(sys.prefix).parent,
            env={key: "untrusted-delivery", "MEIVA_HOME": str(tmp_path)},
        )


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
        )
        sid = row[0]
        assert (
            repair.replace_script(pool, sid, "print(2)\n", repair.script_sha256(row[9]))
            == "updated"
        )
        after = router.fetch_full_blocking(pool, sid)
        assert after[:8] == row[:8]
        assert after[9] == "print(2)\n"
        assert (
            repair.replace_script(pool, sid, "print(2)\n", repair.script_sha256(row[9]))
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
        row = router.create_blocking(pool, router.ScheduleCreate(name="stale", script="pass\n"))
        with pytest.raises(RuntimeError, match="expected old script"):
            repair.replace_script(pool, row[0], "print(2)\n", "0" * 64)
        with pytest.raises(Exception, match="syntax error"):
            repair.replace_script(pool, row[0], "def broken(\n", repair.script_sha256(row[9]))
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
            pool, router.ScheduleCreate(name="external", script="pass\n", enabled=True)
        )
    external = tmp_path / "operator-tool.py"
    external.write_bytes(Path(repair.__file__).read_bytes())
    script_file = tmp_path / "prepared.py"
    script_file.write_bytes(b"raise AssertionError('the body must never run')\r\n")
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")} | {
        "MEIVA_HOME": str(home)
    }
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
            repair.script_sha256(row[9]),
        ],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
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

    def fail(**_arguments: object) -> str:
        raise RuntimeError("unexpected writer failure")

    with pytest.raises(RuntimeError, match="unexpected writer failure"):
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
            ],
            repair_fn=fail,
        )
