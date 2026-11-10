# meiva:note
# Claude Code login keepalive: the pure plan, the round's effects with an injected command runner and
# locator, and the manifest entry. No test starts a real `claude`.
# meiva:link schedules/README.md

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentityReader
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.host.env.harness.convergence import CommandResult
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from schedules.jobs.harness.claude_auth_keepalive import default_command_runner, execute_round
from schedules.plans.harness.claude_keepalive import (
    KEEPALIVE_MODEL,
    KEEPALIVE_PROMPT,
    KEEPALIVE_TIMEOUT_S,
    TIMEOUT_EXIT_CODE,
    decide_run,
    decide_without_run,
    keepalive_argv,
    logged_in,
    login_status_argv,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SLOT = datetime(2026, 11, 10, 5, 10, tzinfo=UTC)
_CLAUDE = "/fake/bin/claude"
_FAKE_KEY = "sk-ant-oat01-ABCDEFGHIJKLMNOP"


def test_manifest_declares_the_keepalive_per_machine() -> None:
    manifest = json.loads((_REPO_ROOT / "schedules" / "manifest.json").read_text())
    entry = next(
        item for item in manifest["builtin_schedules"] if item["name"] == "claude-auth-keepalive"
    )
    assert entry["class"] == "product"
    assert entry["placement"] == "per_machine"
    assert entry["script"] == "jobs/harness/claude-auth-keepalive-schedule.py"
    assert entry["command"] == "python jobs/harness/claude-auth-keepalive-schedule.py"
    assert (_REPO_ROOT / "schedules" / entry["script"]).is_file()


def test_keepalive_command_is_one_cheap_toolless_turn() -> None:
    argv = keepalive_argv(_CLAUDE)
    assert argv[:2] == (_CLAUDE, "-p")
    assert argv[argv.index("--model") + 1] == KEEPALIVE_MODEL == "haiku"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert KEEPALIVE_PROMPT not in argv
    assert 0 < KEEPALIVE_TIMEOUT_S <= 300


def test_the_round_never_names_an_oauth_endpoint_or_a_credentials_path() -> None:
    sources = [
        _REPO_ROOT / "schedules" / "plans" / "harness" / "claude_keepalive.py",
        _REPO_ROOT / "schedules" / "jobs" / "harness" / "claude_auth_keepalive.py",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("oauth/token", "console.anthropic.com", ".credentials.json", "Keychain"):
            assert forbidden not in text.replace("`", ""), f"{path.name} mentions {forbidden}"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"loggedIn": true, "authMethod": "claude.ai"}', True),
        ('{"loggedIn": false}', False),
        ("", None),
        ("not json", None),
        ("[]", None),
        ('{"loggedIn": "yes"}', None),
        ('{"other": 1}', None),
    ],
)
def test_logged_in_reads_only_the_boolean_flag(stdout: str, expected: bool | None) -> None:
    assert logged_in(stdout) is expected


def test_failure_log_keeps_the_last_lines_and_masks_credentials() -> None:
    noise = "\n".join(f"line {i}" for i in range(20))
    text = (
        f"{noise}\nAuthorization: Bearer abc.def-ghi\n"
        f'{{"accessToken": "{_FAKE_KEY}", "refresh_token": "rt-secret-value"}}\n'
        f"key {_FAKE_KEY}\neyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"
    )
    line = decide_run(1, "", text).log_line
    assert _FAKE_KEY not in line
    assert "abc.def-ghi" not in line
    assert "rt-secret-value" not in line
    assert "eyJhbGciOi" not in line
    assert "line 0" not in line
    assert len(line) < 1000


def test_decide_without_run_reasons() -> None:
    assert decide_without_run("non_production").status == "skipped"
    for reason in ("claude_not_found", "not_logged_in"):
        decision = decide_without_run(reason)
        assert decision.status == "failed"
        assert decision.telemetry_payload == {"status": "failed", "reason": reason}


def test_decide_run_success() -> None:
    decision = decide_run(0, "ok", "")
    assert decision.status == "ok"
    assert decision.telemetry_payload == {"status": "ok", "exit_code": 0}


def test_decide_run_failure_carries_exit_code_and_stderr_tail() -> None:
    decision = decide_run(2, "", "a\nb\nsomething broke\n")
    assert decision.status == "failed"
    assert decision.reason == "nonzero_exit"
    assert "exit code 2" in decision.log_line
    assert "something broke" in decision.log_line
    assert decision.telemetry_payload == {
        "status": "failed",
        "reason": "nonzero_exit",
        "exit_code": 2,
    }


def test_decide_run_failure_never_logs_a_token() -> None:
    decision = decide_run(1, "", f"bad key {_FAKE_KEY}")
    assert _FAKE_KEY not in decision.log_line
    assert _FAKE_KEY not in json.dumps(dict(decision.telemetry_payload))


def test_decide_run_failure_falls_back_to_stdout_when_stderr_is_empty() -> None:
    decision = decide_run(1, "Please run /login", "")
    assert "Please run /login" in decision.log_line


def test_decide_run_timeout_is_its_own_reason() -> None:
    decision = decide_run(TIMEOUT_EXIT_CODE, "", "slow")
    assert decision.status == "failed"
    assert decision.reason == "timeout"


def test_default_runner_reports_a_timeout_as_exit_124(tmp_path: Path) -> None:
    result = default_command_runner(
        ["/bin/sleep", "30"],
        timeout=0.5,
        env=os.environ,
        stdin=None,
    )
    assert result.exit_code == TIMEOUT_EXIT_CODE


def test_default_runner_reports_a_missing_program_without_raising(tmp_path: Path) -> None:
    result = default_command_runner(
        [str(tmp_path / "nope")], timeout=5.0, env=os.environ, stdin=None
    )
    assert result.exit_code == 127
    assert "cannot execute" in result.stderr


class _Calls:
    def __init__(self, results: Sequence[CommandResult]) -> None:
        self._results = list(results)
        self.argvs: list[tuple[str, ...]] = []
        self.stdins: list[str | None] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str],
        stdin: str | None,
    ) -> CommandResult:
        self.argvs.append(tuple(argv))
        self.stdins.append(stdin)
        return self._results.pop(0)


def _locate_found(_home: Path, _env: Mapping[str, str]) -> str | None:
    return _CLAUDE


def _locate_missing(_home: Path, _env: Mapping[str, str]) -> str | None:
    return None


_LOGGED_IN = CommandResult(0, '{"loggedIn": true}', "")
_LOGGED_OUT = CommandResult(1, '{"loggedIn": false}', "")


def _inputs(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
) -> ScheduleInputs:
    return ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(_REPO_ROOT, None),
        machine=machine_identity,
        home=tmp_path,
    )


def _run(
    inputs: ScheduleInputs,
    tmp_path: Path,
    *,
    production: bool,
    locate: Callable[[Path, Mapping[str, str]], str | None],
    calls: _Calls,
) -> None:
    execute_round(
        _SLOT,
        None,
        inputs=inputs,
        user_home=tmp_path / "user",
        base_env={"PATH": "/usr/bin:/bin"},
        is_production=production,
        locate=locate,
        run_command=calls,
    )


def test_a_healthy_round_runs_the_status_check_then_one_keepalive(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _Calls([_LOGGED_IN, CommandResult(0, "ok", "")])
    _run(
        _inputs(database, machine_identity, emitted_events, tmp_path),
        tmp_path,
        production=True,
        locate=_locate_found,
        calls=calls,
    )

    assert calls.argvs == [login_status_argv(_CLAUDE), keepalive_argv(_CLAUDE)]
    assert calls.stdins == [None, KEEPALIVE_PROMPT]
    assert "claude-auth-keepalive: ok" in capsys.readouterr().out
    (event,) = emitted_events.named("claude_auth_keepalive_round")
    assert event.attributes == {"status": "ok", "exit_code": 0}


def test_a_missing_binary_is_recorded_and_nothing_runs(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _Calls([])
    _run(
        _inputs(database, machine_identity, emitted_events, tmp_path),
        tmp_path,
        production=True,
        locate=_locate_missing,
        calls=calls,
    )

    assert calls.argvs == []
    assert "not found" in capsys.readouterr().out
    (event,) = emitted_events.named("claude_auth_keepalive_round")
    assert event.attributes == {"status": "failed", "reason": "claude_not_found"}


def test_a_logged_out_machine_is_recorded_and_no_model_call_is_made(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _Calls([_LOGGED_OUT])
    _run(
        _inputs(database, machine_identity, emitted_events, tmp_path),
        tmp_path,
        production=True,
        locate=_locate_found,
        calls=calls,
    )

    assert calls.argvs == [login_status_argv(_CLAUDE)]
    assert "not logged in" in capsys.readouterr().out
    (event,) = emitted_events.named("claude_auth_keepalive_round")
    assert event.attributes == {"status": "failed", "reason": "not_logged_in"}


def test_an_unreadable_status_still_attempts_the_keepalive(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
) -> None:
    calls = _Calls([CommandResult(1, "", "boom"), CommandResult(0, "ok", "")])
    _run(
        _inputs(database, machine_identity, emitted_events, tmp_path),
        tmp_path,
        production=True,
        locate=_locate_found,
        calls=calls,
    )
    assert len(calls.argvs) == 2


def test_a_failing_command_is_logged_with_exit_code_and_does_not_raise(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _Calls([_LOGGED_IN, CommandResult(3, "", f"fatal {_FAKE_KEY}\nstack end")])
    _run(
        _inputs(database, machine_identity, emitted_events, tmp_path),
        tmp_path,
        production=True,
        locate=_locate_found,
        calls=calls,
    )

    out = capsys.readouterr().out
    assert "exit code 3" in out
    assert "stack end" in out
    assert _FAKE_KEY not in out
    (event,) = emitted_events.named("claude_auth_keepalive_round")
    assert event.attributes == {"status": "failed", "reason": "nonzero_exit", "exit_code": 3}


def test_a_non_production_identity_skips_without_running_anything(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _Calls([])
    _run(
        _inputs(database, machine_identity, emitted_events, tmp_path),
        tmp_path,
        production=False,
        locate=_locate_found,
        calls=calls,
    )

    assert calls.argvs == []
    assert "skipped: non-production identity" in capsys.readouterr().out
    (event,) = emitted_events.named("claude_auth_keepalive_round")
    assert event.attributes == {"status": "skipped", "reason": "non_production"}


def test_schedule_script_once_flag_skips_on_a_non_production_home(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MEIVA_MACHINE_NAME=runner-test\n")
    script = _REPO_ROOT / "schedules" / "jobs" / "harness" / "claude-auth-keepalive-schedule.py"
    done = subprocess.run(
        [sys.executable, str(script), "--once"],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_MACHINE_NAME": "runner-test",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "claude-auth-keepalive: skipped: non-production identity" in done.stdout
