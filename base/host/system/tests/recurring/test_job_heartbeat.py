# meiva:note
# The generated job definitions promise a heartbeat, and these assertions read the artifact.
#
# They never ask whether a job is running: that is the scheduler's and the census's question. What
# is asserted here is the generated side — the plist or crontab line a converge writes — because a
# definition that carries no heartbeat is a job nobody can tell apart from one that was removed,
# and no amount of care at registration time survives a later edit that drops the declaration.

from __future__ import annotations

import json
import plistlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from base.host.system import logs_content, packages_content, pr_flow_content, walg_content
from base.host.system.cron import heartbeat as job_heartbeat
from base.host.system.cron import heartbeat_population
from base.host.system.cron.heartbeat import exit_document, render_declaration
from base.host.system.cron.heartbeat_record import record_exit, recording_exit
from base.host.system.job_values import (
    ERROR_LOG_NAME,
    HEARTBEAT_WRITE_FAILED_RC,
    DailyAt,
    DuplicateHeartbeatName,
    HeartbeatDeclaration,
    HeartbeatRender,
    InvalidHeartbeatDeclaration,
    JobHeartbeat,
    JobPaths,
    JobRoots,
    JobSchedule,
    MissingHeartbeatDeclaration,
    NoHeartbeat,
    RenderContext,
)
from base.host.system.tests.recurring.heartbeats import MACHINE, TODAY, heartbeat_for

_MEIVA = "/work tree/.venv/bin/meiva"


def _paths(roots: JobRoots) -> JobPaths:
    return JobPaths(roots, _MEIVA)


def _plist(roots: JobRoots) -> dict[str, object]:
    body = logs_content.launchd_plist_content(
        paths=_paths(roots),
        path_env="/usr/bin:/bin",
        heartbeat=_heartbeat(logs_content.LABEL),
    )
    return plistlib.loads(body.encode())


_SCHEDULES: dict[str, JobSchedule] = {
    logs_content.LABEL: logs_content.schedule(),
    packages_content.LABEL: packages_content.schedule(tick_seconds=900),
    pr_flow_content.LABEL: pr_flow_content.schedule(),
    walg_content.LABEL: walg_content.schedule(backup_hour=3),
}


# meiva:note
# The product a registrar hands a renderer for one of the four generated jobs.
def _heartbeat(label: str) -> HeartbeatRender:
    return heartbeat_for(label, _ROOTS, schedule=_SCHEDULES[label])


_ROOTS = JobRoots(home=Path("/cluster"), repo=Path("/check/out"), user_home=Path("/users/zy"))


# meiva:note
# One render call per generated job, made the way that job's registrar makes it.
def _renders(kind: str) -> Callable[[], str]:
    match kind:
        case "logs":
            return lambda: logs_content.launchd_plist_content(
                paths=_paths(_ROOTS),
                path_env="/usr/bin:/bin",
                heartbeat=_heartbeat(logs_content.LABEL),
            )
        case "packages":
            return lambda: packages_content.launchd_plist_content(
                tick_seconds=900,
                paths=_paths(_ROOTS),
                path_env="/usr/bin:/bin",
                heartbeat=_heartbeat(packages_content.LABEL),
            )
        case "pr-flow":
            return lambda: pr_flow_content.launchd_plist_content(
                paths=_paths(_ROOTS),
                path_env="/usr/bin:/bin",
                heartbeat=_heartbeat(pr_flow_content.LABEL),
            )
        case _:
            return lambda: walg_content.launchd_plist_content(
                backup_hour=3,
                paths=_paths(_ROOTS),
                path_env="/usr/bin:/bin",
                heartbeat=_heartbeat(walg_content.LABEL),
            )


_UNRENDERED = [
    (
        logs_content.LABEL,
        lambda: logs_content.launchd_plist_content(paths=_paths(_ROOTS), path_env="/bin"),
    ),
    (
        packages_content.LABEL,
        lambda: packages_content.launchd_plist_content(
            tick_seconds=900, paths=_paths(_ROOTS), path_env="/bin"
        ),
    ),
    (
        pr_flow_content.LABEL,
        lambda: pr_flow_content.launchd_plist_content(paths=_paths(_ROOTS), path_env="/bin"),
    ),
    (
        walg_content.LABEL,
        lambda: walg_content.launchd_plist_content(
            backup_hour=3, paths=_paths(_ROOTS), path_env="/bin"
        ),
    ),
]


def test_the_plist_carries_the_heartbeat_the_command_writes() -> None:
    plist = _plist(_ROOTS)
    environment = plist["EnvironmentVariables"]
    assert isinstance(environment, dict)

    assert environment["MEIVA_JOB_HEARTBEAT"] == "/cluster/state/heartbeats/logs-maintenance.json"
    assert job_heartbeat.HEARTBEAT_ENV == "MEIVA_JOB_HEARTBEAT"


@pytest.mark.parametrize("kind", ["logs", "packages", "pr-flow", "walg"])
def test_every_generated_job_states_what_the_machine_now_expects(kind: str) -> None:
    body = _renders(kind)()

    # meiva:note
    # The counts are the population, not this job: every artifact states the same declared set,
    # so a reader can see a job that dropped out of it without diffing four documents.
    assert "declared=4 heartbeat=4 none=0 expired=0" in body
    assert "owner=agent:405" in body
    assert "machine=macmini" in body
    assert "domain=gui/501" in body
    assert "dir=/cluster/state/heartbeats" in body
    assert f"<key>{job_heartbeat.HEARTBEAT_ENV}</key>" in body


@pytest.mark.parametrize("kind", ["logs", "packages", "pr-flow", "walg"])
def test_the_cron_entry_carries_the_same_heartbeat_and_keeps_its_marker(kind: str) -> None:
    entries = {
        "logs": lambda: logs_content.cron_entry(
            _paths(_ROOTS), heartbeat=_heartbeat(logs_content.LABEL)
        ),
        "packages": lambda: packages_content.cron_entry(
            _paths(_ROOTS), tick_seconds=900, heartbeat=_heartbeat(packages_content.LABEL)
        ),
        "pr-flow": lambda: pr_flow_content.cron_entry(
            _paths(_ROOTS), heartbeat=_heartbeat(pr_flow_content.LABEL)
        ),
        "walg": lambda: walg_content.cron_entry(
            _paths(_ROOTS), backup_hour=3, heartbeat=_heartbeat(walg_content.LABEL)
        ),
    }
    line = entries[kind]()

    assert "MEIVA_JOB_HEARTBEAT=/cluster/state/heartbeats/" in line
    assert "meiva-heartbeat: declared=4" in line
    # meiva:note
    # The marker stays the line's trailer: it is what a rewrite matches on, and the heartbeat
    # statement goes before it so a rewrite still finds the line.
    assert line.rstrip().endswith(
        {
            "logs": "# meiva-logs-maintenance",
            "packages": "# meiva-packages-refresh",
            "pr-flow": "# meiva-pr-flow",
            "walg": "# meiva-walg",
        }[kind]
    )


@pytest.mark.parametrize(("label", "render"), _UNRENDERED)
def test_a_job_whose_heartbeat_is_not_declared_does_not_render(
    label: str, render: Callable[[], str]
) -> None:
    with pytest.raises(MissingHeartbeatDeclaration, match=label):
        render()


def test_a_generated_job_outside_the_declared_population_does_not_render() -> None:
    with pytest.raises(MissingHeartbeatDeclaration, match=r"com\.meiva\.something-new"):
        heartbeat_population.verified(
            "com.meiva.something-new",
            roots=_ROOTS,
            machine=MACHINE,
            uid=501,
            schedule=DailyAt(hour=1, minute=0),
            today=TODAY,
        )


def test_every_generated_job_module_declares_a_heartbeat() -> None:
    package = Path(heartbeat_population.__file__).parent.parent
    # meiva:note
    # The population is compared against the directory, not trusted: a new `*_content.py` that
    # nobody listed here fails this assertion, which is the CI half of the render-time refusal in
    # `heartbeat_population.verified`.
    generated = (logs_content, packages_content, pr_flow_content, walg_content)
    files = {path.stem for path in package.glob("*_content.py")}
    assert files == {module.__name__.rsplit(".", 1)[-1] for module in generated}

    for module in generated:
        assert isinstance(module.HEARTBEAT, (JobHeartbeat, NoHeartbeat)), module.__name__

    assert {module.LABEL for module in generated} == {
        label for label, _ in heartbeat_population.POPULATION
    }
    # meiva:note
    # The counts only move by consent: adding a generated job, or declaring an exception, has to
    # edit the numbers asserted here and in the artifact test, and that edit is the review.
    document = heartbeat_population.declarations_document(
        machine=MACHINE,
        repo_sha=None,
        roots=_ROOTS,
        uid=501,
        policy=_policy(),
        today=TODAY,
    )
    jobs = cast("list[dict[str, object]]", document["jobs"])
    assert len(jobs) == 4
    assert all(job["heartbeat_name"] for job in jobs)


def _context() -> RenderContext:
    return RenderContext(home=_ROOTS.home, machine=MACHINE, uid=501, today=TODAY)


def _policy() -> heartbeat_population.MachinePolicy:
    return heartbeat_population.MachinePolicy(
        jobs_enabled=True,
        owned_home=True,
        packages_refresh=True,
        packages_tick_seconds=900,
        walg_configured=True,
        production=True,
        backup_hour=3,
    )


def test_two_jobs_declaring_one_heartbeat_name_do_not_render() -> None:
    shared = JobHeartbeat(name="logs-maintenance", owner="agent:405", late_after_seconds=60)
    population: tuple[tuple[str, HeartbeatDeclaration | None], ...] = (
        ("com.meiva.alpha", shared),
        ("com.meiva.beta", shared),
    )

    with pytest.raises(DuplicateHeartbeatName) as raised:
        render_declaration(
            job="com.meiva.alpha",
            declaration=shared,
            population=population,
            schedule=DailyAt(hour=1, minute=0),
            context=_context(),
        )
    message = str(raised.value)
    assert "com.meiva.alpha" in message and "com.meiva.beta" in message


_LATER = TODAY + timedelta(days=23)


@pytest.mark.parametrize(
    ("exception", "missing"),
    [
        (NoHeartbeat(owner="agent:405", until=_LATER), "reason"),
        (NoHeartbeat(reason="watched elsewhere", until=_LATER), "owner"),
        (NoHeartbeat(reason="watched elsewhere", owner="agent:405"), "until"),
    ],
)
def test_an_exception_without_its_three_parts_does_not_render(
    exception: NoHeartbeat, missing: str
) -> None:
    population: tuple[tuple[str, HeartbeatDeclaration | None], ...] = (
        (logs_content.LABEL, exception),
    )
    with pytest.raises(InvalidHeartbeatDeclaration, match=missing):
        render_declaration(
            job=logs_content.LABEL,
            declaration=exception,
            population=population,
            schedule=logs_content.schedule(),
            context=_context(),
        )


def test_an_exception_inside_its_window_warns_before_it_expires() -> None:
    exception = NoHeartbeat(
        reason="watched by the helper", owner="agent:6111", until=TODAY + timedelta(days=7)
    )
    render = render_declaration(
        job=logs_content.LABEL,
        declaration=exception,
        population=((logs_content.LABEL, exception),),
        schedule=logs_content.schedule(),
        context=_context(),
    )

    assert "none-expiring:com.meiva.logs-maintenance in 7 days" in render.artifact
    assert "heartbeat=0 none=1 expired=0" in render.artifact
    assert render.environment == ()
    assert render.file is None


def test_an_expired_exception_still_renders_and_says_so() -> None:
    exception = NoHeartbeat(
        reason="watched by the helper", owner="agent:6111", until=TODAY - timedelta(days=7)
    )
    render = render_declaration(
        job=logs_content.LABEL,
        declaration=exception,
        population=((logs_content.LABEL, exception),),
        schedule=logs_content.schedule(),
        context=_context(),
    )

    # meiva:note
    # Expired is reported, never enforced. A refused render would fail a converge on the day of
    # expiry and hand the failure to whoever happens to be deploying, who is not the owner.
    assert "none=0 expired=1" in render.artifact
    assert "expired:com.meiva.logs-maintenance" in render.artifact
    assert "owner=agent:6111" in render.artifact
    assert "reason=watched by the helper" in render.artifact


def test_the_exit_document_matches_what_a_reader_parses() -> None:
    document = json.loads(
        exit_document(name="logs-maintenance", pid=4242, updated_at=1794109668.42, rc=0)
    )

    assert document == {
        "schema": 1,
        "name": "logs-maintenance",
        "pid": 4242,
        "updated_at": 1794109668.42,
        "ok": True,
        "rc": 0,
    }
    assert (
        json.loads(
            exit_document(name="logs-maintenance", pid=4242, updated_at=1794109668.42, rc=19)
        )["ok"]
        is False
    )


@pytest.mark.parametrize(
    ("pid", "updated_at"),
    [(0, 1794109668.42), (4242, 0.0), (4242, float("inf"))],
)
def test_a_heartbeat_records_a_real_process_at_a_real_instant(pid: int, updated_at: float) -> None:
    with pytest.raises(ValueError):
        exit_document(name="logs-maintenance", pid=pid, updated_at=updated_at, rc=0)


def test_a_finished_run_writes_its_heartbeat(tmp_path: Path) -> None:
    target = tmp_path / "state" / "heartbeats" / "logs-maintenance.json"

    code = record_exit(
        environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
        pid=4242,
        now=datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        rc=19,
    )

    assert code == 19
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema": 1,
        "name": "logs-maintenance",
        "pid": 4242,
        "updated_at": datetime(2026, 11, 8, 4, 41, tzinfo=UTC).timestamp(),
        "ok": False,
        "rc": 19,
    }


def test_a_run_without_a_heartbeat_to_write_keeps_its_own_exit_code(tmp_path: Path) -> None:
    assert record_exit(environ={}, pid=4242, now=datetime.now(UTC), rc=7) == 7
    assert list(tmp_path.iterdir()) == []


# meiva:note
# A heartbeat path whose directory cannot be made: `heartbeats` is a file, not a directory, so the
# `mkdir` a write would do fails while the degraded channel beside it stays writable.
def _blocked_heartbeat(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    _ = (state / "heartbeats").write_text("", encoding="utf-8")
    return state / "heartbeats" / "logs-maintenance.json"


def _degraded_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "state" / ERROR_LOG_NAME
    return log.read_text(encoding="utf-8").splitlines()


def test_a_success_that_cannot_be_recorded_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _blocked_heartbeat(tmp_path)

    code = record_exit(
        environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
        pid=4242,
        now=datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        rc=0,
    )

    assert code == HEARTBEAT_WRITE_FAILED_RC == 3
    err = capsys.readouterr().err
    assert "heartbeat write failed (cannot write the job heartbeat" in err
    assert "the run's success cannot be reported, exiting 3" in err
    lines = _degraded_lines(tmp_path)
    assert len(lines) == 1
    assert "\tlogs-maintenance\trc=0\t" in lines[0]


def test_a_failing_run_keeps_its_own_code_when_the_record_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _blocked_heartbeat(tmp_path)

    code = record_exit(
        environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
        pid=4242,
        now=datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        rc=19,
    )

    assert code == 19
    assert "job exit code 19 preserved" in capsys.readouterr().err
    assert "\tlogs-maintenance\trc=19\t" in _degraded_lines(tmp_path)[0]


def test_a_raising_run_keeps_its_own_code_when_the_record_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _blocked_heartbeat(tmp_path)

    def broken() -> int:
        raise RuntimeError("the maintenance pass failed")

    with pytest.raises(RuntimeError, match="the maintenance pass failed"):
        _ = recording_exit(
            broken,
            environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
            pid=4242,
            clock=lambda: datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        )

    assert "job exit code 1 preserved" in capsys.readouterr().err
    assert "\tlogs-maintenance\trc=1\t" in _degraded_lines(tmp_path)[0]


def test_a_system_exit_claims_nothing_it_could_not_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _blocked_heartbeat(tmp_path)

    def succeeded() -> int:
        raise SystemExit(0)

    with pytest.raises(SystemExit) as raised:
        _ = recording_exit(
            succeeded,
            environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
            pid=4242,
            clock=lambda: datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        )

    assert raised.value.code == HEARTBEAT_WRITE_FAILED_RC
    assert "the run's success cannot be reported" in capsys.readouterr().err


def test_a_system_exit_keeps_the_code_the_command_chose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _blocked_heartbeat(tmp_path)

    def stopped(rc: int) -> int:
        raise SystemExit(rc)

    with pytest.raises(SystemExit) as raised:
        _ = recording_exit(
            lambda: stopped(4),
            environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
            pid=4242,
            clock=lambda: datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        )

    assert raised.value.code == 4
    assert "job exit code 4 preserved" in capsys.readouterr().err


def test_a_raising_run_is_recorded_before_the_error_propagates(tmp_path: Path) -> None:
    target = tmp_path / "state" / "heartbeats" / "logs-maintenance.json"

    def broken() -> int:
        raise RuntimeError("the maintenance pass failed")

    with pytest.raises(RuntimeError, match="the maintenance pass failed"):
        _ = recording_exit(
            broken,
            environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
            pid=4242,
            clock=lambda: datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        )

    assert json.loads(target.read_text(encoding="utf-8"))["ok"] is False


def test_a_clean_run_is_recorded_with_its_own_code(tmp_path: Path) -> None:
    target = tmp_path / "state" / "heartbeats" / "logs-maintenance.json"

    assert (
        recording_exit(
            lambda: 0,
            environ={job_heartbeat.HEARTBEAT_ENV: str(target)},
            pid=4242,
            clock=lambda: datetime(2026, 11, 8, 4, 41, tzinfo=UTC),
        )
        == 0
    )
    record = json.loads(target.read_text(encoding="utf-8"))
    assert (record["ok"], record["rc"], record["name"]) == (True, 0, "logs-maintenance")
    assert record["pid"] == 4242
