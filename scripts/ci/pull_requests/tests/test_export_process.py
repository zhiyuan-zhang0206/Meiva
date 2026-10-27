import ast
import inspect
import subprocess
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.config import db_config_from_settings
from base.host.proc import run_bounded
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
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
def test_writer_uses_one_captured_gate_and_is_joined(known: bool, tmp_path: Path) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "commit", "--allow-empty", "-m", "captured")
    captured = LoadedCommit.capture(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-m", "later-head")
    image = captured if known else LoadedCommit(tmp_path, None)
    with pytest.raises(ValueError, match="loaded image"):
        export_process.process_gate("exporter", image=None)
    gate = export_process.process_gate("exporter", image=image)
    config = ConfigBoot()
    config.set_field("machine_name", "export-owner")
    machine = ConfiguredMachineIdentity(config)
    assert machine.name() == "export-owner"
    config.set_field("machine_name", "changed-after-first-read")
    assert machine.name() == "export-owner"
    direct_url = ClusterDirectUrl(machine.host, home=meiva_home())
    assert direct_url.local_host == machine.host
    db = Database(db_config_from_settings(), gate=gate, direct_url=direct_url)
    assert db is not None
    with export_process.drain_event_pipeline(
        "exporter", EventPipeline(writer=lambda _events: None)
    ) as pipeline:
        assert not pipeline.stopped
        if not known:
            with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
                gate.application_name()
        else:
            assert gate.application_name() == "meiva:exporter:v1"
            (tmp_path / ".git").rename(tmp_path / "captured-history")
            assert gate.application_name() == "meiva:exporter:v1"

    assert pipeline.stopped


def test_body_error_preserves_the_original_writer_error_during_close() -> None:
    primary = ValueError("export failed")
    worker_error = OSError("writer failed")

    def write(_events: list[Event]) -> None:
        raise worker_error

    pipeline = EventPipeline(writer=write, batch_size=1)
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
        export_process.drain_event_pipeline("exporter", pipeline) as owned,
    ):
        owned.enqueue(event)
        raise primary
    assert caught.value is primary and pipeline.stopped
    assert any("writer failed" in note for note in primary.__notes__)
    with pytest.raises(OSError) as worker:
        pipeline.stop(timeout=0)
    assert worker.value is worker_error


def test_export_entry_captures_before_fetch_or_borrows_the_supplied_writer(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    emitted: list[Event] = []

    def write(events: list[Event]) -> None:
        calls.append("emit")
        emitted.extend(events)

    pipeline = EventPipeline(writer=write)
    commands: list[list[str]] = []

    def run_process(
        argv: Sequence[str], *, timeout: float, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 60 and capture_output and text
        assert list(argv[:4]) == ["gh", "api", "--method", "GET"]
        if not commands:
            calls.append("fetch")
        commands.append(list(argv))
        assert "actions/runs?" in argv[4] or "pulls?" in argv[4]
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    try:
        assert (
            runs_export.main(
                ["--repo", "owner/repo", "--state-dir", str(tmp_path)],
                producer=lambda: pipeline,
                run_process=run_process,
                timers=ManualTimers(),
                home=tmp_path,
            )
            == 0
        )
        assert calls == ["fetch", "emit"]
        assert not pipeline.stopped
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


def test_the_owned_writer_takes_the_home_it_is_given_and_reads_none() -> None:
    source = inspect.getsource(export_process.owned_event_pipeline)
    function = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(function, ast.FunctionDef)
    names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    assert "meiva_home" not in names
    homes = [
        keyword.value
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "home"
    ]
    assert len(homes) == 2
    assert all(isinstance(value, ast.Name) and value.id == "home" for value in homes)
    assert [arg.arg for arg in function.args.kwonlyargs] == ["image", "home"]
    assert not function.args.kw_defaults[1]
