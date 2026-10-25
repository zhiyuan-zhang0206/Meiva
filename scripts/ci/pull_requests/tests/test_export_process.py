import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_settings
from base.host.proc import run_bounded
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import Event, EventPipeline
from scripts.ci.pull_requests import export_process, runs_export


# meiva:note
# These test-owned git commands build the temporary history whose captured version is asserted.
def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=C2 Test", "-c", "user.email=c2@example.test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("known", [True, False])
def test_writer_uses_one_captured_gate_and_is_joined(
    monkeypatch: pytest.MonkeyPatch, known: bool, tmp_path: Path
) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "commit", "--allow-empty", "-m", "captured")
    captured = LoadedCommit.capture(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-m", "later-head")
    image = captured if known else LoadedCommit(tmp_path, None)
    gates: list[ProcessDbGate] = []
    hosts: list[Callable[[], str]] = []
    config = ConfigBoot()
    config.set_field("machine_name", "export-owner")
    monkeypatch.setattr(export_process, "ConfigBoot", lambda: config)
    monkeypatch.setattr(export_process, "meiva_home", lambda: tmp_path)

    def handle(*, gate: ProcessDbGate, direct_url: ClusterDirectUrl) -> Database:
        gates.append(gate)
        hosts.append(direct_url.local_host)
        return Database(db_config_from_settings(), gate=gate, direct_url=direct_url)

    def build(
        *, database: Callable[[], Database], machine: MachineIdentityReader, home: Path
    ) -> EventPipeline:
        assert machine.name() == "export-owner"
        assert home == tmp_path
        config.set_field("machine_name", "changed-after-first-read")
        assert machine.name() == "export-owner"
        database()
        database()
        assert hosts == [machine.host, machine.host]
        return EventPipeline(writer=lambda _events: None)

    monkeypatch.setattr(Database, "from_settings", handle)
    monkeypatch.setattr(export_process, "build_pipeline", build)
    with export_process.owned_event_pipeline("exporter", image=image) as pipeline:
        assert not pipeline.stopped
        assert len(gates) == 2 and gates[0] is gates[1]
        if not known:
            with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
                gates[0].application_name()
        else:
            assert gates[0].application_name() == "meiva:exporter:v1"
            (tmp_path / ".git").rename(tmp_path / "captured-history")
            assert gates[1].application_name() == "meiva:exporter:v1"

    assert pipeline.stopped


def test_body_error_preserves_the_original_writer_error_during_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError("export failed")
    worker_error = OSError("writer failed")

    def write(_events: list[Event]) -> None:
        raise worker_error

    pipeline = EventPipeline(writer=write, batch_size=1)

    def build(
        *, database: Callable[[], Database], machine: MachineIdentityReader, home: Path
    ) -> EventPipeline:
        assert callable(database)
        return pipeline

    monkeypatch.setattr(export_process, "build_pipeline", build)
    event = Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="exporter",
        category="telemetry",
        event_name="pr_flow_run",
        level="info",
        source="test",
        target_agent_id=None,
    )
    with (
        pytest.raises(ValueError) as caught,
        export_process.owned_event_pipeline(
            "exporter", image=LoadedCommit(Path("/loaded"), None)
        ) as owned,
    ):
        owned.enqueue(event)
        raise primary
    assert caught.value is primary and pipeline.stopped
    assert any("writer failed" in note for note in primary.__notes__)
    with pytest.raises(OSError) as worker:
        pipeline.stop(timeout=0)
    assert worker.value is worker_error


@pytest.mark.parametrize("borrowed", [False, True])
def test_export_entry_captures_before_fetch_or_borrows_the_supplied_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, borrowed: bool
) -> None:
    calls: list[str] = []
    image = LoadedCommit(Path("/loaded-exporter"), None)
    emitted: list[Event] = []

    def write(events: list[Event]) -> None:
        calls.append("emit")
        emitted.extend(events)

    pipeline = EventPipeline(writer=write)

    def capture(source_root: Path) -> LoadedCommit:
        assert not borrowed
        calls.append("capture")
        return image

    commands: list[list[str]] = []

    def run_process(
        argv: Sequence[str], *, timeout: float, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 60 and capture_output and text
        assert list(argv[:4]) == ["gh", "api", "--method", "GET"]
        if not commands:
            assert calls == ([] if borrowed else ["capture"])
            calls.append("fetch")
        commands.append(list(argv))
        assert "actions/runs?" in argv[4] or "pulls?" in argv[4]
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    def build(
        *, database: Callable[[], Database], machine: MachineIdentityReader, home: Path
    ) -> EventPipeline:
        assert not borrowed and callable(database)
        calls.append("build")
        return pipeline

    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(export_process, "build_pipeline", build)
    try:
        assert (
            runs_export.main(
                ["--repo", "owner/repo", "--state-dir", str(tmp_path)],
                producer=(lambda: pipeline) if borrowed else None,
                run_process=run_process,
                timers=ManualTimers(),
            )
            == 0
        )
        assert calls == (["fetch", "emit"] if borrowed else ["capture", "fetch", "build", "emit"])
        assert pipeline.stopped is not borrowed
        assert len(commands) == 2
        assert {event.event_name for event in emitted} == {"ci_runs_daily", "ci_runs_run"}
        assert all(event.process == runs_export.PROCESS_NAME for event in emitted)
        assert (tmp_path / "owner--repo.json").is_file()
        assert (tmp_path / "snapshot.json").is_file()
    finally:
        pipeline.stop(timeout=2)


@pytest.mark.parametrize("mode", ["leaf", "api"])
def test_command_type_import_does_not_start_the_exporter(mode: str, tmp_path: Path) -> None:
    import sys

    user = tmp_path / "user"
    user.mkdir()
    home = tmp_path / "meiva"
    source = Path(__file__).parents[1] / "gh_process.py"
    result = run_bounded(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "scripts.ci.pull_requests.tests.gh_process_import_child",
            mode,
            str(source),
        ],
        env={
            "HOME": str(user),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not home.exists()
