from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.daemon import health, health_schema
from base.daemon.tests.health_support import unknown_image
from base.host.env import port_lease
from base.native_process.os_platform import file_lock
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.backup.layout import BackupTree
from services.backup.scheduler import daemon
from services.backup.scheduler.recovery_drill import (
    load_local_dump_restore_success,
    record_local_dump_restore_success,
)
from services.backup.scheduler.settings import SchedulerSettings
from services.backup.scheduler.tests.operation_support import STUB_WORKER
from services.backup.scheduler.worker import WorkerLaunch, run_job

_SUNDAY_WINDOW = datetime(2026, 8, 30, 3, tzinfo=UTC)


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _find_free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _settings(*, hour: int = 0) -> SchedulerSettings:
    return SchedulerSettings(
        timezone=ZoneInfo("UTC"), backup_hour=hour, keep=7, throwaway_base=None
    )


def _launch(mode: str, log: Path) -> WorkerLaunch:
    return WorkerLaunch(STUB_WORKER, {"STUB_MODE": mode, "STUB_LOG": str(log)})


def _ran(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_module_entrypoint_runs_the_scheduler(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "MEIVA_DB_URL=postgresql://meiva:test@127.0.0.1:1/meiva\n"
        "MEIVA_REDIS_URL=redis://meiva:test@127.0.0.1:1/0\n"
    )
    env = dict(os.environ)
    env.update({"MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"})
    result = subprocess.run(
        [sys.executable, "-m", "services.backup.scheduler.daemon"],
        cwd=Path(__file__).parents[4],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "Traceback" in result.stderr
    assert "assert_schema_current" in result.stderr, result.stderr


async def _http_get(port: int) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), body


def test_backup_components_hold_a_boot_grace_then_require_a_fresh_success() -> None:
    state = daemon.BackupState(started_at=100.0)
    wall = 1_000_000.0
    grace_end = 100.0 + daemon.BACKUP_STALE_AFTER_S

    assert daemon.backup_components(state, wall_time=wall, monotonic=grace_end) == [
        {"name": "backup", "status": "ok", "progress": "idle"}
    ]

    assert daemon.backup_components(state, wall_time=wall, monotonic=grace_end + 1.0) == [
        {
            "name": "backup",
            "status": "degraded",
            "detail": "no successful backup within 93601s of start",
            "progress": "idle",
        }
    ]

    succeeded_at = datetime.fromtimestamp(wall, tz=UTC)
    state.record_success(succeeded_at)
    assert daemon.backup_components(state, wall_time=wall + 10.0, monotonic=0.0) == [
        {
            "name": "backup",
            "status": "ok",
            "last_success": wall,
            "age_s": 10.0,
            "progress": "idle",
        }
    ]
    stale = daemon.backup_components(
        state, wall_time=wall + daemon.BACKUP_STALE_AFTER_S + 1.0, monotonic=0.0
    )
    assert stale[0]["status"] == "degraded"


def test_backup_components_keep_running_dump_healthy_after_previous_failure() -> None:
    wall = 1_000_000.0
    state = daemon.BackupState(started_at=0.0, running=True)
    state.record_attempt(datetime.fromtimestamp(wall, tz=UTC))
    state.record_error("disk full")

    assert daemon.backup_components(state, wall_time=wall + 1.0, monotonic=0.0) == [
        {
            "name": "backup",
            "status": "ok",
            "last_error": "disk full",
            "progress": "running 1s",
        }
    ]


@pytest.mark.asyncio
async def test_healthz_returns_503_for_an_overdue_backup() -> None:
    state = daemon.BackupState(started_at=0.0)
    port = _find_free_port()
    server = await health.start_health_server(
        "pg_backup",
        port=port,
        components=lambda: daemon.backup_components(
            state, wall_time=0.0, monotonic=daemon.BACKUP_STALE_AFTER_S + 1.0
        ),
        image=unknown_image(),
    )
    try:
        status, body = await _http_get(port)
        assert status == 503
        assert json.loads(body)["degraded_reasons"] == [
            "backup: no successful backup within 93601s of start"
        ]
    finally:
        await health.stop_health_server(server)


def test_failed_dump_does_not_make_healthz_ask_the_supervisor_for_a_restart() -> None:
    state = daemon.BackupState(started_at=0.0)
    state.record_error("disk full")

    components = daemon.backup_components(state, wall_time=10.0, monotonic=10.0)

    assert components[0]["status"] == "ok"
    assert components[0]["last_error"] == "disk full"
    assert "last dump failed: disk full" in str(components[0]["detail"])
    status, _body = health_schema.render({"name": "pg_backup"}, components)
    assert status == 200


def test_the_settings_are_read_once_from_their_owner() -> None:
    from base.config import ConfigBoot

    config = ConfigBoot()
    config.view.general.timezone = "Asia/Shanghai"
    config.view.services.backup_hour = 7
    config.view.services.backup_keep = 5
    config.view.data_plane.pg_throwaway_base = ""

    settings = SchedulerSettings.read(config)
    config.view.services.backup_hour = 9

    assert settings == SchedulerSettings(
        timezone=ZoneInfo("Asia/Shanghai"), backup_hour=7, keep=5, throwaway_base=None
    )


def test_a_relative_throwaway_base_is_captured_as_an_absolute_path() -> None:
    from base.config import ConfigBoot

    config = ConfigBoot()
    config.view.data_plane.pg_throwaway_base = "scratch-base"

    base = SchedulerSettings.read(config).throwaway_base

    assert base is not None and base.is_absolute() and base.name == "scratch-base"


async def test_a_due_backup_runs_once_then_the_round_waits_for_tomorrow(tmp_path: Path) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"
    state = daemon.BackupState(started_at=0.0)
    config = _settings()
    launch = _launch("succeed", log)

    first = await daemon.backup_round(state, settings=config, tree=tree, launch=launch)

    [published] = tree.managed.glob("*.dump.enc")
    assert _ran(log) == ["dump", "restore"], "a successful dump is followed by the due drill"
    assert state.running is False and state.last_error is None
    assert state.last_success is not None
    assert 0 < first <= 24 * 3600
    assert published.read_bytes().startswith(b"encrypted ")
    assert not list(tree.controls("dump").glob(".operation-*"))
    assert load_local_dump_restore_success(tree.restore_marker) is not None

    second = await daemon.backup_round(state, settings=config, tree=tree, launch=launch)

    assert _ran(log) == ["dump", "restore"], "today's dump exists, so no second worker is launched"
    assert 0 < second <= 24 * 3600


async def test_a_failed_backup_is_reported_and_not_retried_before_the_next_backup_hour(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"
    state = daemon.BackupState(started_at=0.0)

    sleep_s = await daemon.backup_round(
        state, settings=_settings(), tree=tree, launch=_launch("fail", log)
    )

    assert _ran(log) == ["fail"]
    assert 0 < sleep_s <= 24 * 3600
    assert state.running is False
    assert state.last_error is not None and "operation exited 3" in state.last_error
    assert state.last_success is None
    [event] = emitted_events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "backup_dump_failed",
        "error",
    )
    assert event.attributes["detail"] == state.last_error


async def test_a_backup_with_todays_dump_launches_nothing_and_sleeps_until_the_next_hour(
    tmp_path: Path,
) -> None:
    log, tree = tmp_path / "workers.log", BackupTree(tmp_path / "home")
    tree.managed.mkdir(parents=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (tree.managed / f"stubdb-{stamp}.dump.enc").write_bytes(b"x")

    sleep_s = await daemon.backup_round(
        daemon.BackupState(started_at=0.0),
        settings=_settings(),
        tree=tree,
        launch=_launch("succeed", log),
    )

    assert _ran(log) == []
    assert 0 < sleep_s <= 24 * 3600


async def test_the_due_local_restore_drill_runs_and_records_its_success(tmp_path: Path) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"

    await daemon.local_restore_round(
        _SUNDAY_WINDOW, settings=_settings(hour=3), tree=tree, launch=_launch("restore", log)
    )

    assert _ran(log) == ["restore"]
    assert load_local_dump_restore_success(tree.restore_marker) == _SUNDAY_WINDOW
    assert not list(tree.controls("restore-drill").glob(".operation-*"))


async def test_a_local_restore_drill_already_recorded_this_week_does_not_run(
    tmp_path: Path,
) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"
    record_local_dump_restore_success(_SUNDAY_WINDOW, tree.restore_marker)

    await daemon.local_restore_round(
        _SUNDAY_WINDOW, settings=_settings(hour=3), tree=tree, launch=_launch("restore", log)
    )

    assert _ran(log) == []


async def test_a_local_restore_drill_before_its_window_does_not_run(tmp_path: Path) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"
    record_local_dump_restore_success(datetime(2026, 8, 23, 3, tzinfo=UTC), tree.restore_marker)

    await daemon.local_restore_round(
        datetime(2026, 8, 30, 2, 59, tzinfo=UTC),
        settings=_settings(hour=3),
        tree=tree,
        launch=_launch("restore", log),
    )

    assert _ran(log) == []


async def test_a_local_restore_drill_failure_is_reported_without_publishing_success(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"

    await daemon.local_restore_round(
        _SUNDAY_WINDOW, settings=_settings(hour=3), tree=tree, launch=_launch("fail", log)
    )

    assert _ran(log) == ["fail"]
    assert load_local_dump_restore_success(tree.restore_marker) is None
    [event] = emitted_events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "recovery_drill_failed",
        "error",
    )
    assert event.attributes["drill"] == "logical_dump"
    assert "operation exited 3" in str(event.attributes["detail"])


async def test_an_invalid_local_restore_marker_reports_failure_without_running_restore(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"
    tree.restore_marker.parent.mkdir(parents=True)
    tree.restore_marker.write_text("not a marker")

    await daemon.local_restore_round(
        _SUNDAY_WINDOW, settings=_settings(hour=3), tree=tree, launch=_launch("restore", log)
    )

    assert _ran(log) == []
    [event] = emitted_events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "recovery_drill_failed",
        "error",
    )
    assert event.attributes == {
        "drill": "logical_dump",
        "detail": "logical restore drill success marker is invalid",
    }


async def test_a_busy_restore_kind_defers_the_drill_without_a_failure_event(
    tmp_path: Path, emitted_events: EmittedEvents, caplog: pytest.LogCaptureFixture
) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"
    controls = tree.controls("restore-drill")
    controls.mkdir(parents=True)
    caplog.set_level("INFO", logger="services.backup.scheduler.daemon")

    with file_lock(controls / ".lock"):
        await daemon.local_restore_round(
            _SUNDAY_WINDOW, settings=_settings(hour=3), tree=tree, launch=_launch("restore", log)
        )

    assert _ran(log) == []
    assert list(emitted_events) == []
    assert "local restore drill deferred" in caplog.text
    assert load_local_dump_restore_success(tree.restore_marker) is None


def _staged_dump(tmp_path: Path) -> tuple[Path, BackupTree, str]:
    staged = tmp_path / "controls" / "meiva-20260926T000000Z.dump.enc"
    staged.parent.mkdir()
    staged.write_bytes(b"encrypted")
    return staged, BackupTree(tmp_path / "home"), hashlib.sha256(b"encrypted").hexdigest()


def test_the_scheduled_commit_publishes_a_private_artifact_and_never_replaces_one(
    tmp_path: Path,
) -> None:
    from services.backup.scheduler import worker

    staged, tree, digest = _staged_dump(tmp_path)

    target = worker.commit_scheduled_backup(staged, digest, tree=tree, keep=7)

    assert target.parent == tree.managed
    assert target.read_bytes() == b"encrypted" and target.stat().st_mode & 0o777 == 0o600
    assert not staged.exists()
    staged.write_bytes(b"encrypted")
    with pytest.raises(FileExistsError):
        worker.commit_scheduled_backup(staged, digest, tree=tree, keep=7)
    assert staged.exists() and [path.name for path in tree.managed.iterdir()] == [target.name]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("notes.txt", b"encrypted", "not a managed regular artifact"),
        ("meiva-20260926T000000Z.dump.enc", b"tampered", "changed after preparation"),
    ],
)
def test_the_scheduled_commit_refuses_what_the_worker_did_not_prepare(
    tmp_path: Path, name: str, content: bytes, message: str
) -> None:
    from services.backup.scheduler import worker

    staged, tree, digest = _staged_dump(tmp_path)
    other = staged.with_name(name)
    other.write_bytes(content)

    with pytest.raises(RuntimeError, match=message):
        worker.commit_scheduled_backup(other, digest, tree=tree, keep=7)

    assert other.exists()


def test_a_cross_filesystem_copy_is_a_verified_private_publication(tmp_path: Path) -> None:
    from services.backup.scheduler import worker

    staged, tree, digest = _staged_dump(tmp_path)
    tree.managed.mkdir(parents=True)
    target = tree.managed / staged.name

    worker.publish_copy(staged, target, digest)

    assert target.read_bytes() == b"encrypted" and target.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in tree.managed.iterdir()] == [target.name]
    with pytest.raises(FileExistsError):
        worker.publish_copy(staged, target, digest)
    assert [path.name for path in tree.managed.iterdir()] == [target.name]
    with pytest.raises(RuntimeError, match="copy differs"):
        worker.publish_copy(staged, tree.managed / "other.dump.enc", "0" * 64)
    assert [path.name for path in tree.managed.iterdir()] == [target.name]


def test_a_cross_filesystem_copy_survives_a_concurrent_sweep_until_linked(tmp_path: Path) -> None:
    from services.backup.artifact.intermediates import sweep_closed_partials
    from services.backup.scheduler import worker

    staged, tree, digest = _staged_dump(tmp_path)
    tree.managed.mkdir(parents=True)
    target = tree.managed / staged.name

    with worker.private_copy(staged, tree.managed, target.name, digest) as copy:
        sweep_closed_partials(tree.managed)
        assert copy.read_bytes() == b"encrypted"
        os.link(copy, target)

    assert target.read_bytes() == b"encrypted"
    assert [path.name for path in tree.managed.iterdir()] == [target.name]


def test_the_commit_defers_pruning_while_another_backup_holds_the_lock(tmp_path: Path) -> None:
    from services.backup.scheduler import worker
    from services.backup.tests.test_backup import (
        await_backup_lock_holder,
        spawn_backup_lock_holder,
    )

    staged, tree, digest = _staged_dump(tmp_path)
    tree.managed.mkdir(parents=True)
    oldest = tree.managed / "stubdb-20260101T000000Z.dump.enc"
    oldest.write_bytes(b"older")
    ready = tmp_path / "ready"
    holder = spawn_backup_lock_holder(tree.home, ready, hold_s=30.0)
    try:
        await_backup_lock_holder(ready, holder)

        target = worker.commit_scheduled_backup(staged, digest, tree=tree, keep=1)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert target.read_bytes() == b"encrypted"
    assert oldest.exists(), "pruning waits for the next backup instead of stalling the scheduler"


async def test_run_job_commits_the_workers_dump_into_the_pool(tmp_path: Path) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"

    await run_job(
        "dump",
        settings=_settings(),
        tree=tree,
        launch=_launch("dump", log),
        now=datetime(2026, 9, 26, 3, tzinfo=UTC),
    )

    [published] = tree.managed.iterdir()
    assert published.name == "stubdb-20260926T030000Z.dump.enc"
    assert published.stat().st_mode & 0o777 == 0o600


async def test_run_job_refuses_a_restore_result_of_another_shape(tmp_path: Path) -> None:
    tree, log = BackupTree(tmp_path / "home"), tmp_path / "workers.log"

    with pytest.raises(RuntimeError, match="invalid result"):
        await run_job("restore", settings=_settings(), tree=tree, launch=_launch("ok", log))

    assert not list(tree.controls("restore-drill").glob(".operation-*"))
