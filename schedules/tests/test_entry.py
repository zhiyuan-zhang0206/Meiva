# meiva:note
# The schedule entry against a real git checkout and the throwaway Postgres.
#
# A fresh entry names its database sessions after the commit its process loaded, so a checkout
# that moves on underneath a running schedule never relabels it; a borrowed entry leaves the
# runner's database handle, event writer and SDK context alone.

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

import meiva
from base.cluster.machine import MachineIdentityReader
from base.config import settings
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from schedules.jobs.entry import cluster_timezone, fresh_schedule_entry, schedule_entry


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_commits(repo: Path, count: int) -> LoadedCommit:
    _git(repo, "init", "--initial-branch=main")
    for number in range(count):
        _git(repo, "commit", "--allow-empty", "-m", f"commit {number}")
    return LoadedCommit.capture(repo)


def _writer_threads() -> int:
    return sum(thread.name == "event-emitter" for thread in threading.enumerate())


def test_quiet_schedule_exit_never_builds_a_writer(tmp_path: Path) -> None:
    image = _repo_with_commits(tmp_path, 1)
    before = _writer_threads()

    with fresh_schedule_entry(
        image=image, process="schedule-probe", home=tmp_path / "home"
    ) as inputs:
        assert callable(inputs.producer)
        assert meiva.context.require_machine() is inputs.machine
        assert _writer_threads() == before


def test_a_fresh_entry_binds_a_context_whose_event_bus_is_built_on_first_read(
    tmp_path: Path,
) -> None:
    image = _repo_with_commits(tmp_path, 1)

    with fresh_schedule_entry(image=image, process="schedule-probe", home=tmp_path / "home"):
        context = meiva.context
        assert context.bus is None
        bus = context.require_bus()
        assert isinstance(bus, EventBus)
        assert context.require_bus() is bus


def test_schedule_closes_constructed_writer_and_preserves_loop_error(tmp_path: Path) -> None:
    image = _repo_with_commits(tmp_path, 1)
    primary = RuntimeError("original schedule loop error")
    pipeline = None
    prior = getattr(meiva, "context", None)

    with (
        pytest.raises(RuntimeError) as caught,
        fresh_schedule_entry(
            image=image, process="schedule-probe", home=tmp_path / "home"
        ) as inputs,
    ):
        pipeline = inputs.producer()
        assert inputs.producer() is pipeline
        assert meiva.context.require_machine() is inputs.machine
        raise primary

    assert caught.value is primary
    assert pipeline is not None and pipeline.stopped
    assert getattr(meiva, "context", None) is prior


# meiva:note
# The gate counts first-parent commits up to the SHA the process loaded. The checkout gains a
# commit after the capture, and the session still names the older count.
def test_database_sessions_name_the_image_loaded_not_the_checkout_now(
    tmp_path: Path, db_url: str
) -> None:
    image = _repo_with_commits(tmp_path, 3)
    _git(tmp_path, "commit", "--allow-empty", "-m", "the checkout moved on")

    with (
        fresh_schedule_entry(
            image=image, process="schedule-probe", home=tmp_path / "home"
        ) as inputs,
        inputs.database().connect(autocommit=True) as conn,
    ):
        row = conn.execute("SHOW application_name").fetchone()

    assert row == ("meiva:schedule-probe:v3",)


def test_runner_inputs_are_borrowed_without_rebuilding_or_closing(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
) -> None:
    pipeline = emitted_events.producer()
    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(Path("/entry"), None),
        machine=machine_identity,
        home=Path("/entry-home"),
    )
    prior = getattr(meiva, "context", None)
    primary = RuntimeError("borrowed loop failed")

    with pytest.raises(RuntimeError) as caught, schedule_entry(inputs) as borrowed:
        assert borrowed is inputs
        assert borrowed.database() is database
        assert getattr(meiva, "context", None) is prior
        raise primary

    assert caught.value is primary
    assert not pipeline.stopped
    with database.connect(autocommit=True) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_cluster_timezone_is_the_cluster_setting() -> None:
    assert cluster_timezone() == settings.general.timezone
