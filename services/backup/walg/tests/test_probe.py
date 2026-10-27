from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.paths import meiva_home
from services.backup.walg import config as walg_config
from services.backup.walg import probe
from services.backup.walg import state as walg_state
from services.backup.walg.archive import expected_archive
from services.backup.walg.archive_plan import ARCHIVE_TIMEOUT_S, RPO_OBJECTIVE_S
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.tests.support import (
    SECRETS,
    NeverDialed,
    PgInstance,
    Sandbox,
    archiving_postgres,
    make_sandbox,
    refused_target,
    wait_for,
    write_some_wal_and_switch,
)

_ALL_TEXTS = (
    probe.CONFIG_UNUSABLE,
    probe.KEY_CHANGED,
    probe.SETTINGS_DIFFER,
    probe.ARCHIVER_FAILING,
    probe.ARCHIVE_BEHIND,
    probe.UNREADABLE,
    probe.TICK_NOT_RUNNING,
    probe.CHAIN_BROKEN,
    probe.TICK_STATE_UNREADABLE,
    probe.RUN_FAILED,
    probe.DRILL_FAILED,
    probe.DRILL_OVERDUE,
    *probe.RUN_FAILED_AT.values(),
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _healthy_state(sandbox: Sandbox) -> probe.ArchiverState:
    expected = expected_archive(path=sandbox.path, home=meiva_home())
    assert expected is not None
    return probe.ArchiverState(
        archive_mode=expected.mode,
        archive_command=expected.command,
        archive_timeout_s=expected.timeout_s,
        failing_now=False,
        archived_count=10,
        failed_count=0,
        oldest_pending_age_s=None,
    )


def test_failure_texts_carry_no_number_that_could_change_between_runs() -> None:
    for text in _ALL_TEXTS:
        assert not any(char.isdigit() for char in text), text


def test_a_matching_quiet_archiver_is_healthy(sandbox: Sandbox) -> None:
    expected = expected_archive(path=sandbox.path, home=meiva_home())
    assert expected is not None

    assert probe.judge(_healthy_state(sandbox), expected) is None


@pytest.mark.parametrize(
    "drift",
    [
        {"archive_mode": "off"},
        {"archive_timeout_s": ARCHIVE_TIMEOUT_S + 1},
        {"archive_command": "/old/path/wal-g --config /old.json wal-push %p"},
    ],
)
def test_postgres_running_other_archive_settings_is_reported(
    sandbox: Sandbox, drift: dict[str, Any]
) -> None:
    expected = expected_archive(path=sandbox.path, home=meiva_home())
    assert expected is not None

    assert probe.judge(replace(_healthy_state(sandbox), **drift), expected) == probe.SETTINGS_DIFFER


def test_a_failing_archiver_is_reported(sandbox: Sandbox) -> None:
    expected = expected_archive(path=sandbox.path, home=meiva_home())
    assert expected is not None

    state = replace(_healthy_state(sandbox), failing_now=True)

    assert probe.judge(state, expected) == probe.ARCHIVER_FAILING


def test_pending_wal_is_late_only_beyond_the_rpo_objective(sandbox: Sandbox) -> None:
    expected = expected_archive(path=sandbox.path, home=meiva_home())
    assert expected is not None
    healthy = _healthy_state(sandbox)

    assert probe.judge(replace(healthy, oldest_pending_age_s=0.0), expected) is None
    assert probe.judge(replace(healthy, oldest_pending_age_s=RPO_OBJECTIVE_S), expected) is None
    assert (
        probe.judge(replace(healthy, oldest_pending_age_s=RPO_OBJECTIVE_S + 1), expected)
        == probe.ARCHIVE_BEHIND
    )


def test_settings_drift_outranks_the_archiver_conditions(sandbox: Sandbox) -> None:
    expected = expected_archive(path=sandbox.path, home=meiva_home())
    assert expected is not None
    state = replace(
        _healthy_state(sandbox), archive_mode="off", failing_now=True, oldest_pending_age_s=10_000.0
    )

    assert probe.judge(state, expected) == probe.SETTINGS_DIFFER


def test_off_is_silent_and_never_dials_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)

    assert probe.failure_on(sandbox.path, NeverDialed(), now=NOW, home=meiva_home()) is None
    assert probe.configuration_failure(sandbox.path) is None


def test_a_valid_configuration_is_fine(sandbox: Sandbox) -> None:
    assert probe.configuration_failure(sandbox.path) is None


def test_an_unusable_configuration_is_reported_without_its_contents(sandbox: Sandbox) -> None:
    sandbox.config_file.write_text("{" + "".join(SECRETS))

    assert probe.configuration_failure(sandbox.path) == probe.CONFIG_UNUSABLE


def test_a_swapped_key_is_reported_once_a_key_is_pinned(sandbox: Sandbox) -> None:
    sandbox.key_file.write_text("cd" * 32 + "\n")
    assert probe.configuration_failure(sandbox.path) is None, (
        "nothing is pinned yet: reading never pins"
    )

    walg_config.load_walg_config(path=sandbox.path)
    assert probe.configuration_failure(sandbox.path) is None

    sandbox.key_file.write_text("ef" * 32 + "\n")
    assert probe.configuration_failure(sandbox.path) == probe.KEY_CHANGED


def test_the_probe_never_raises_when_postgres_cannot_be_read(
    sandbox: Sandbox, postgres_access: None
) -> None:
    unreachable = probe.admin_connection_to(refused_target())

    assert (
        probe.failure_on(sandbox.path, unreachable, now=NOW, home=meiva_home()) == probe.UNREADABLE
    )


def test_a_healthy_archiver_reads_healthy_and_an_idle_one_is_not_late(
    sandbox: Sandbox, postgres_access: None
) -> None:
    with archiving_postgres(sandbox=sandbox) as pg, pg.connect() as conn:
        write_some_wal_and_switch(conn)

        def settled() -> bool:
            state = probe.read_archiver_state(conn)
            return state.archived_count >= 1 and state.oldest_pending_age_s is None

        wait_for(settled, what="the closed segment to be archived")
        time.sleep(2.5)

        state = probe.read_archiver_state(conn)
        expected = expected_archive(path=sandbox.path, home=meiva_home())
        assert expected is not None
        assert (state.archive_mode, state.archive_command) == ("on", expected.command)
        assert (state.archive_timeout_s, state.failing_now, state.failed_count) == (
            ARCHIVE_TIMEOUT_S,
            False,
            0,
        )
        assert probe.judge(state, expected, rpo_objective_s=1) is None
        assert _failure(sandbox, pg, rpo_objective_s=1) is None


def test_a_failing_archive_command_is_reported_with_work_waiting(
    sandbox: Sandbox, postgres_access: None
) -> None:
    sandbox.set_mode("fail")
    with archiving_postgres(sandbox=sandbox) as pg, pg.connect() as conn:
        write_some_wal_and_switch(conn)

        wait_for(lambda: probe.read_archiver_state(conn).failing_now, what="a failure")

        state = probe.read_archiver_state(conn)
        assert state.oldest_pending_age_s is not None, "the failed segment stays queued"
        assert _failure(sandbox, pg) == probe.ARCHIVER_FAILING


def test_a_hung_archive_command_is_caught_by_the_age_of_the_waiting_wal(
    sandbox: Sandbox, postgres_access: None
) -> None:
    sandbox.set_mode("hang")
    with archiving_postgres(sandbox=sandbox) as pg, pg.connect() as conn:
        write_some_wal_and_switch(conn)

        wait_for(
            lambda: probe.read_archiver_state(conn).oldest_pending_age_s is not None,
            what="the closed segment to be queued",
        )
        assert _failure(sandbox, pg, rpo_objective_s=1000) is None, (
            "freshly queued work is not late yet"
        )

        wait_for(
            lambda: _failure(sandbox, pg, rpo_objective_s=2) == probe.ARCHIVE_BEHIND,
            seconds=30,
            what="the segment to be late",
        )
        state = probe.read_archiver_state(conn)
        assert state.failing_now is False
        sandbox.set_mode("ok")


def test_the_application_login_cannot_list_the_archive_queue(
    sandbox: Sandbox, postgres_access: None
) -> None:
    with archiving_postgres(sandbox=sandbox) as pg:
        with pg.connect() as admin:
            admin.execute("CREATE ROLE app LOGIN")
        with (
            psycopg.connect(
                host=str(pg.socket_dir),
                port=pg.port,
                user="app",
                dbname="postgres",
                autocommit=True,
            ) as app,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            app.execute("SELECT count(*) FROM pg_ls_archive_statusdir()")


def test_the_admin_dial_is_custody_checked_and_bounded(
    sandbox: Sandbox, tmp_path: Path, postgres_access: None
) -> None:
    with archiving_postgres(sandbox=sandbox) as pg:
        with probe.admin_connection_to(pg.target) as conn:
            row = conn.execute("SHOW statement_timeout").fetchone()
            assert row is not None and row[0] == "10s"
        foreign = PgTarget(pg.target.admin_url, tmp_path / "another-home" / "pg", "postgres")
        with pytest.raises((RuntimeError, OSError)), probe.admin_connection_to(foreign):
            pytest.fail("a data directory that is not this postmaster's must refuse the dial")


NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def _failure(
    sandbox: Sandbox, pg: PgInstance, *, rpo_objective_s: float = RPO_OBJECTIVE_S
) -> str | None:
    return probe.failure_on(
        sandbox.path,
        probe.admin_connection_to(pg.target),
        now=datetime.now(UTC),
        rpo_objective_s=rpo_objective_s,
        home=meiva_home(),
    )


def _run(status: walg_state.RunStatus = "ok", step: str | None = None) -> walg_state.RunRecord:
    return walg_state.RunRecord(
        started_at=NOW - timedelta(hours=1),
        finished_at=NOW - timedelta(minutes=30),
        status=status,
        step=step,
        detail="d",
    )


def _tick(age: timedelta, skipped: str | None = None) -> walg_state.TickRecord:
    return walg_state.TickRecord(started_at=NOW - age, skipped=skipped)


def _verify(integrity: str, timeline: str = "OK") -> walg_state.VerifyRecord:
    return walg_state.VerifyRecord(at=NOW, integrity=integrity, timeline=timeline)


def _judge(recorded: walg_state.State, *, since: datetime | None = None) -> str | None:
    return probe.tick_judge(recorded, NOW, enabled_since=since)


def test_a_recent_successful_tick_is_healthy() -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=1)), run=_run(), verify=_verify("OK"))

    assert _judge(recorded) is None


def test_a_warning_from_wal_verify_is_not_a_chain_failure() -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=1)), verify=_verify("WARNING"))

    assert _judge(recorded) is None


@pytest.mark.parametrize(("integrity", "timeline"), [("FAILURE", "OK"), ("OK", "FAILURE")])
def test_a_broken_chain_is_reported_whichever_check_found_it(integrity: str, timeline: str) -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1)), run=_run(), verify=_verify(integrity, timeline)
    )

    assert _judge(recorded) == probe.CHAIN_BROKEN


def test_a_chain_failure_outranks_the_failed_run_it_caused() -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1)),
        run=_run("failed", walg_state.STEP_VERIFY),
        verify=_verify("FAILURE"),
    )

    assert _judge(recorded) == probe.CHAIN_BROKEN


@pytest.mark.parametrize("step", walg_state.STEPS)
def test_a_failed_run_is_reported_with_its_step(step: str) -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=1)), run=_run("failed", step))

    assert _judge(recorded) == probe.RUN_FAILED_AT[step]


def test_every_step_has_its_own_text() -> None:
    assert set(probe.RUN_FAILED_AT) == set(walg_state.STEPS)
    assert len(set(probe.RUN_FAILED_AT.values())) == len(walg_state.STEPS)


def test_a_failed_run_stays_reported_while_later_ticks_only_skip() -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1), skipped="a deploy window is open"),
        run=_run("failed", walg_state.STEP_BACKUP),
    )

    assert _judge(recorded) == probe.RUN_FAILED_AT[walg_state.STEP_BACKUP]


def test_a_tick_that_started_within_a_period_is_running() -> None:
    just_inside = timedelta(hours=24) - timedelta(seconds=1)

    assert _judge(walg_state.State(tick=_tick(just_inside))) is None
    assert _judge(walg_state.State(tick=_tick(timedelta(hours=24)))) is None


def test_no_tick_for_longer_than_a_period_is_reported() -> None:
    just_outside = timedelta(hours=24) + timedelta(seconds=1)

    assert _judge(walg_state.State(tick=_tick(just_outside))) == probe.TICK_NOT_RUNNING


def test_a_skipped_tick_counts_as_a_tick_that_ran() -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=2), skipped="postgres is down"))

    assert _judge(recorded) is None


def test_before_any_tick_the_time_since_enabling_is_what_counts() -> None:
    empty = walg_state.State()

    assert _judge(empty) is None, "no tick and no enabling time: nothing to compare"
    assert _judge(empty, since=NOW - timedelta(hours=23)) is None
    assert _judge(empty, since=NOW - timedelta(hours=25)) == probe.TICK_NOT_RUNNING


def test_the_enabling_time_is_the_key_pins_mtime(sandbox: Sandbox) -> None:
    walg_config.load_walg_config(path=sandbox.path)
    assert probe.enabled_since() is not None
    pinned_at = datetime(2026, 10, 1, 0, 0, tzinfo=UTC).timestamp()
    os.utime(walg_config.key_id_path(), (pinned_at, pinned_at))

    assert probe.tick_failure(now=datetime(2026, 10, 3, 0, 0, tzinfo=UTC)) == probe.TICK_NOT_RUNNING
    assert probe.tick_failure(now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC)) is None


def test_without_a_pin_there_is_no_enabling_time(sandbox: Sandbox) -> None:
    assert probe.enabled_since() is None
    assert probe.tick_failure(now=NOW) is None


def test_tick_failure_reads_the_state_file(sandbox: Sandbox) -> None:
    walg_state.write_state(
        walg_state.State(tick=_tick(timedelta(hours=1)), run=_run("failed", walg_state.STEP_VERIFY))
    )

    assert probe.tick_failure(now=NOW) == probe.RUN_FAILED_AT[walg_state.STEP_VERIFY]


def test_an_unreadable_state_file_is_its_own_fixed_failure_not_an_exception(
    sandbox: Sandbox,
) -> None:
    walg_state.state_path().parent.mkdir(parents=True)
    walg_state.state_path().write_text("{broken " + "".join(SECRETS))

    assert probe.tick_failure(now=NOW) == probe.TICK_STATE_UNREADABLE


def _drill(
    *, ok: bool = True, last_ok_age: timedelta | None = timedelta(days=1)
) -> walg_state.DrillRecord:
    return walg_state.DrillRecord(
        finished_at=NOW - timedelta(hours=1),
        ok=ok,
        backup="base_1",
        target_lsn="0/A3000000",
        seconds=600.0,
        detail="d",
        last_ok_at=None if last_ok_age is None else NOW - last_ok_age,
    )


def _with_drill(drill: walg_state.DrillRecord | None) -> walg_state.State:
    return walg_state.State(tick=_tick(timedelta(hours=1)), drill=drill)


def test_a_drill_that_succeeded_within_its_period_is_healthy() -> None:
    assert _judge(_with_drill(_drill())) is None
    assert _judge(_with_drill(_drill(last_ok_age=timedelta(days=7)))) is None


def test_a_failed_latest_drill_is_reported_even_with_a_recent_earlier_success() -> None:
    assert _judge(_with_drill(_drill(ok=False))) == probe.DRILL_FAILED
    assert _judge(_with_drill(_drill(ok=False, last_ok_age=None))) == probe.DRILL_FAILED


def test_a_drill_is_overdue_one_tick_after_its_period() -> None:
    deadline = probe.DRILL_PERIOD + probe.TICK_PERIOD

    assert _judge(_with_drill(_drill(last_ok_age=deadline))) is None
    assert (
        _judge(_with_drill(_drill(last_ok_age=deadline + timedelta(seconds=1))))
        == probe.DRILL_OVERDUE
    )


def test_before_any_drill_the_time_since_enabling_counts() -> None:
    since = NOW - (probe.DRILL_PERIOD + probe.TICK_PERIOD + timedelta(seconds=1))

    assert _judge(_with_drill(None), since=since) == probe.DRILL_OVERDUE
    assert _judge(_with_drill(None), since=NOW - timedelta(days=3)) is None
    assert _judge(_with_drill(None), since=None) is None


def test_the_tick_conditions_outrank_the_drill_conditions() -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1)),
        run=_run("failed", walg_state.STEP_BACKUP),
        drill=_drill(ok=False),
    )

    assert _judge(recorded) == probe.RUN_FAILED_AT[walg_state.STEP_BACKUP]


def test_failure_reports_the_tick_when_the_archiver_is_fine(
    sandbox: Sandbox, postgres_access: None
) -> None:
    now = datetime.now(UTC)
    walg_state.write_state(
        walg_state.State(
            tick=walg_state.TickRecord(started_at=now),
            run=walg_state.RunRecord(
                started_at=now,
                finished_at=now,
                status="failed",
                step=walg_state.STEP_RETENTION,
                detail="d",
            ),
        )
    )

    with archiving_postgres(sandbox=sandbox) as pg:
        assert _failure(sandbox, pg) == probe.RUN_FAILED_AT[walg_state.STEP_RETENTION]


def test_an_archiver_failure_outranks_a_tick_failure(
    sandbox: Sandbox, postgres_access: None
) -> None:
    sandbox.set_mode("fail")
    walg_state.write_state(
        walg_state.State(verify=_verify("FAILURE"), tick=walg_state.TickRecord(started_at=NOW))
    )

    with archiving_postgres(sandbox=sandbox) as pg, pg.connect() as conn:
        write_some_wal_and_switch(conn)
        wait_for(lambda: probe.read_archiver_state(conn).failing_now, what="a failure")

        assert _failure(sandbox, pg) == probe.ARCHIVER_FAILING


def test_off_does_not_read_the_tick_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)
    walg_state.walg_dir().mkdir(parents=True, exist_ok=True)
    walg_state.state_path().write_text("{broken")

    assert probe.failure_on(sandbox.path, NeverDialed(), now=NOW, home=meiva_home()) is None
