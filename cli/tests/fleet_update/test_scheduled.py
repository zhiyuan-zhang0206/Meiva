from __future__ import annotations

import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from cli.fleet_update.scheduling.agents import notify, require_quiescent, running_agents
from cli.fleet_update.scheduling.plan import directory_page, initiator_id, run_at
from cli.fleet_update.session import RefusedError


class Gateway:
    def __init__(self, pages: list[str]) -> None:
        self.pages = iter(pages)
        self.sent: list[list[str]] = []

    def run(self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None) -> str:
        if effect:
            self.sent.append(shlex.split(script))
            return "sent"
        return next(self.pages)


def test_relative_and_iso_deadlines() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert run_at("+60s", now) == now + timedelta(minutes=1)
    assert run_at("+10m", now) == now + timedelta(minutes=10)
    assert run_at("2030-01-02T08:00:00+08:00", now) == now + timedelta(days=1)
    for bad in ("+0s", "+-1s", "2030-01-02T00:00:00", "2029-01-01T00:00:00Z"):
        with pytest.raises(ValueError):
            run_at(bad, now)


def test_initiator_requires_explicit_user_or_agent() -> None:
    assert initiator_id("user") is None
    assert initiator_id("agent:9876") == 9876
    for bad in ("", "agent:0", "external_agent:worker", "agent:-1"):
        with pytest.raises(ValueError):
            initiator_id(bad)


def test_all_pages_are_checked_and_sleeping_agents_receive_no_warning() -> None:
    gateway = Gateway(
        [
            "id status machine label\n9 running a worker\n8 idling a sleeper\n(more agents: repeat with --before-id 8)",
            "id status machine label\n7 running b worker",
        ]
    )
    targets = [agent for agent in running_agents(gateway, "gw") if agent != 9]
    notify(gateway, "gw", targets, "agent:9", "finish; 'quoted' $message")
    assert len(gateway.sent) == 1
    assert gateway.sent[0][-5:] == ["send", "7", "finish; 'quoted' $message", "--source", "agent:9"]


def test_strict_gate_notifies_initiator_and_busy_agents_before_refusing() -> None:
    gateway = Gateway(["id status machine label\n7 running a worker\n6 idling a sleeping"])
    with pytest.raises(RefusedError, match="running agents \\[7\\]"):
        require_quiescent(gateway, "gw", "agent:8", "operation")
    assert [row[-4] for row in gateway.sent] == ["7", "8"]


def test_user_abort_and_empty_roster() -> None:
    gateway = Gateway(["(no agents)"])
    require_quiescent(gateway, "gw", "user", "operation")
    assert gateway.sent == []
    gateway = Gateway(["id status machine label\n7 running a worker"])
    with pytest.raises(RefusedError):
        require_quiescent(gateway, "gw", "user", "operation")
    assert len(gateway.sent) == 1


def test_unreadable_directory_fails_closed() -> None:
    for bad in ("", "gateway offline", "id status machine label\n7 unknown a worker"):
        with pytest.raises(ValueError):
            directory_page(bad)


def test_timer_commands_and_private_record(tmp_path: Path) -> None:
    import json
    import plistlib

    from cli.fleet_update.scheduling.host_timer import arm, manage, save

    tool = tmp_path / "launchctl"
    calls = tmp_path / "calls"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$CALLS"\n')
    tool.chmod(0o700)
    record = tmp_path / "operation.json"
    save(record, {"state": "pending", "initiator": "agent:9876"})
    assert record.stat().st_mode & 0o777 == 0o600
    environment = {"PATH": str(tmp_path), "CALLS": str(calls)}
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    arm(
        record,
        command=["/python", "-m", "worker", str(record)],
        moment=moment,
        env=environment,
        system="Darwin",
        uid=123,
    )
    job = cast(dict[str, object], plistlib.loads(record.with_suffix(".plist").read_bytes()))
    assert job["ProgramArguments"] == ["/python", "-m", "worker", str(record)]
    assert job["RunAtLoad"] and not job["KeepAlive"]
    assert calls.read_text().splitlines()[:2] == ["bootstrap", "gui/123"]
    assert manage(record, "cancel", env=environment, system="Darwin", uid=123) == 0
    assert json.loads(record.read_text())["state"] == "cancelled"
    assert "bootout" in calls.read_text()
    assert not Path(str(record) + ".tmp").exists()


def test_linux_timer_and_execution_cannot_be_cancelled(tmp_path: Path) -> None:
    import fcntl

    from cli.fleet_update.scheduling.host_timer import arm, manage, save

    tool = tmp_path / "systemd-run"
    calls = tmp_path / "calls"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$CALLS"\n')
    tool.chmod(0o700)
    record = tmp_path / "operation.json"
    save(record, {"state": "pending"})
    environment = {"PATH": str(tmp_path), "CALLS": str(calls)}
    arm(
        record,
        command=["/bin/true"],
        moment=datetime(2030, 1, 1, tzinfo=UTC),
        env=environment,
        system="Linux",
        uid=123,
    )
    assert "--on-calendar=2030-01-01 00:00:00 UTC" in calls.read_text()
    with record.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert manage(record, "cancel", env=environment, system="Linux", uid=123) == 2


def test_persisted_choices_roundtrip_and_completed_job_never_replays(tmp_path: Path) -> None:
    from dataclasses import asdict, replace

    from cli.fleet_update.choices import DownChoices, FleetArgs
    from cli.fleet_update.scheduled_worker import Record, execute
    from cli.fleet_update.scheduling import validate
    from cli.fleet_update.scheduling.host_timer import save

    args = FleetArgs(
        half="run",
        gateway="gw",
        runner=("runner",),
        log_dir=tmp_path,
        dry_run=False,
        selection="--all-services",
        named={},
        start_timeout=300,
        roster_timeout=60,
        target_request=None,
        message_text="planned update",
        rebuild_extras=False,
        down=DownChoices(
            "--close-shell-sessions",
            "--stop-browser",
            "--stop-extras",
            "--keep-infra",
            "--keep-no-services",
            "smooth",
            300,
            1,
            "--refuse-python-change",
            "--refuse-red-schedules",
        ),
        at="+60s",
        source="agent:9876",
    )
    assert (
        validate(replace(args, at="+999999999999999999999d"), datetime(2030, 1, 1, tzinfo=UTC))
        is not None
    )
    path = tmp_path / "operation.json"
    save(
        path,
        {
            "operation_id": "operation",
            "run_at": "2030-01-01T00:00:00Z",
            "initiator": "agent:9876",
            "new": "a" * 40,
            "repo": str(tmp_path),
            "arguments": asdict(args),
            "state": "complete",
        },
    )
    record = Record.model_validate_json(path.read_text())
    assert record.arguments == args
    assert execute(path) == 0
