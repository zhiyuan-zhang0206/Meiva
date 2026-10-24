from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from base.agents.messages.external_caller import launch_caller_assignment
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus
from meiva.shell.coding_tools import antigravity, claude
from meiva.shell.coding_tools.plans.claude_launch import (
    ClaudeLaunchFacts,
    claude_command,
    plan_claude_launch,
)
from meiva.shell.coding_tools.plans.launch_request import LaunchRequest, checked_prompt
from meiva.shell.coding_tools.plans.pacing import Pacing
from meiva.shell.coding_tools.tests.fake_tools import install_claude, site_for
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions

_SKILL_DIR = (
    Path(__file__).parents[4]
    / "meiva_builtins"
    / "skills"
    / "platform"
    / "meiva-guide"
    / "external-agents"
)
_FAST = Pacing(timeout=30.0, poll=0.05, settle=0.1)
_STAMP = "20261024-120000"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_claude = _load("spawn_claude_under_test", _SKILL_DIR / "scripts" / "spawn_claude.py")
spawn_antigravity = _load(
    "spawn_antigravity_under_test", _SKILL_DIR / "scripts" / "spawn_antigravity.py"
)


def _owner(tmp_path: Path) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(
        workspace,
        tool="claude",
        cluster=tmp_path / "cluster",
    )
    generation = "11111111-2222-3333-4444-555555555555"
    now = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    return coding_session_owner.CodingSessionOwner(
        key=key,
        status=CodingSessionStatus.ACTIVE,
        generation=generation,
        owner_agent_id=41,
        display_label="workspace",
        expected_suffix="claude-workspace-11111111",
        session_id=7,
        session_name="meiva-agent-41-shell-7-claude-workspace-11111111",
        state_dir=coding_session_owner.generation_state_dir(key, generation),
        tasks_file=None,
        work_file=None,
        created_at=now,
        expires_at=now + dt.timedelta(hours=24),
    )


def _request(
    workspace: Path,
    *,
    prompt: str | None = None,
    supervised: bool = False,
    resume: str | None = None,
    provenance: str | None = None,
) -> LaunchRequest:
    tasks_file = workspace / "tasks.md" if supervised else None
    work_file = workspace / "work.md" if supervised else None
    return LaunchRequest(
        workspace=workspace,
        tasks_file=tasks_file,
        work_file=work_file,
        ttl_seconds=3600,
        caller_instance=None,
        prompt=checked_prompt(prompt, tasks_file, work_file),
        skill_dir=_SKILL_DIR,
        resume=resume,
        provenance=provenance,
    )


def _facts(marker_dir: Path, *, session: str = "s-1", assignment: str = "") -> ClaudeLaunchFacts:
    marker_dir.mkdir(exist_ok=True)
    return ClaudeLaunchFacts(
        claude_session=session,
        failure_marker=marker_dir / "missing-claude",
        stamp=_STAMP,
        caller_assignment=assignment,
    )


def test_launch_command_unsets_api_key_and_skips_permission_prompts(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = claude_command(Path(record.key.workspace))

    assert command.startswith(f"cd {record.key.workspace} && ")
    assert "unset ANTHROPIC_API_KEY && " in command
    assert command.endswith('exec "$claude_bin" --dangerously-skip-permissions || exit $?')
    assert "MEIVA_CALLER_IDENTITY" not in command


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = claude_command(
        Path(record.key.workspace), launch_caller_assignment("claude_code", "run-42")
    )

    assert "MEIVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"claude_code"' in command
    assert '"instance":"run-42"' in command


def test_a_prompt_launch_plans_no_files_and_pastes_the_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = "Please run meiva impersonate request --agent 41 --name 'Fix login' --provider claude"

    plan = plan_claude_launch(_request(workspace, prompt=prompt), _facts(tmp_path / "marker"))

    assert plan.files is None
    assert plan.message == prompt
    assert plan.via_paste is True
    assert plan.session_name == "claude-workspace"
    assert plan.ttl_seconds == 3600
    assert plan.launch_line.startswith(f"cd {workspace.as_posix()} && ")


def test_a_supervised_launch_plans_its_files_and_a_bootstrap_message(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    plan = plan_claude_launch(
        _request(workspace, supervised=True, provenance="## Who dispatched you\nby 41"),
        _facts(tmp_path / "marker"),
    )

    assert plan.files is not None
    assert (plan.files.tasks_file, plan.files.work_file) == (
        workspace / "tasks.md",
        workspace / "work.md",
    )
    assert plan.via_paste is False
    assert plan.message.startswith("## Who dispatched you\nby 41\n\n")
    assert "Read the collaboration contract at" in plan.message
    assert str(plan.files.work_file) in plan.message
    assert plan.provenance == "## Who dispatched you\nby 41"


@pytest.mark.parametrize("resume", [None, "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5"])
def test_a_launch_pins_or_reopens_its_claude_session(resume: str | None, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = resume or "11111111-2222-3333-4444-555555555555"

    plan = plan_claude_launch(
        _request(workspace, supervised=True, resume=resume), _facts(tmp_path / "m", session=session)
    )

    flag = "--resume" if resume else "--session-id"
    assert plan.launch_line.endswith(f"--dangerously-skip-permissions {flag} {session} || exit $?")
    assert plan.claude_session == session
    assert plan.resumed is (resume is not None)
    assert ("interrupted" in plan.message) is (resume is not None)


def test_the_launch_line_is_given_a_private_marker_for_a_missing_executable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    facts = _facts(tmp_path / "marker")

    plan = plan_claude_launch(_request(workspace, prompt="hi"), facts)

    assert plan.failure_marker == facts.failure_marker
    assert facts.failure_marker.as_posix() in plan.launch_line
    assert facts.failure_marker.with_name("not-logged-in").as_posix() in plan.launch_line


def _wait_for_text(path: Path, text: str, seconds: float = 20.0) -> str:
    deadline = time.monotonic() + seconds
    content = ""
    while time.monotonic() < deadline:
        content = path.read_text() if path.exists() else ""
        if text in content:
            return content
        time.sleep(0.05)
    return content


def test_a_prompt_launch_delivers_the_prompt_without_creating_files(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    received = short_dir / "received.txt"
    install_claude(unit_home, received)
    workspace = short_dir / "workspace"
    workspace.mkdir()
    prompt = "Please run meiva impersonate request --agent 41 --name 'Fix login' --provider claude"
    plan = replace(
        plan_claude_launch(_request(workspace, prompt=prompt), _facts(short_dir / "marker")),
        ready=_FAST,
    )

    assert claude.run_supervised_launch(site_for(live_shell, unit_home, live_host), plan) == 0

    assert _wait_for_text(received, prompt) == prompt + "\n"
    assert sorted(path.name for path in workspace.iterdir()) == []
    trusted = json.loads((unit_home / ".claude.json").read_text())
    assert trusted["projects"][workspace.as_posix()]["hasTrustDialogAccepted"] is True
    assert live_shell.list() == {0: "claude-workspace"}


def test_a_supervised_launch_prepares_its_files_and_delivers_the_contract_after_the_panel(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    short_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received = short_dir / "received.txt"
    install_claude(unit_home, received, panel_delay=0.5)
    workspace = short_dir / "workspace"
    workspace.mkdir()
    block = "## Who dispatched you\ndispatched_by: agent 41"
    plan = replace(
        plan_claude_launch(
            _request(workspace, supervised=True, provenance=block), _facts(short_dir / "marker")
        ),
        ready=_FAST,
    )

    assert claude.run_supervised_launch(site_for(live_shell, unit_home, live_host), plan) == 0

    delivered = _wait_for_text(received, "Now read the task file and start working.")
    assert delivered.startswith(block.replace("\n", " ")) or block.splitlines()[0] in delivered
    assert "Read the collaboration contract at" in delivered
    assert (workspace / "work.md").read_text() == "STATUS: WORKING\n\n## Log\n\n"
    assert (workspace / "tasks.md").read_text().startswith(block)
    out = capsys.readouterr().out
    assert "session_id=0" in out
    assert "claude_session=s-1" in out
    assert f"tasks_file={workspace / 'tasks.md'}" in out


def test_a_session_name_already_taken_refuses_the_launch(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    short_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received = short_dir / "received.txt"
    install_claude(unit_home, received)
    workspace = short_dir / "workspace"
    workspace.mkdir()
    site = site_for(live_shell, unit_home, live_host)
    taken = site.open_session("claude-workspace", 120)
    plan = replace(
        plan_claude_launch(_request(workspace, prompt="hi"), _facts(short_dir / "marker")),
        ready=_FAST,
    )

    assert claude.run_supervised_launch(site, plan) == 1

    assert "session 'claude-workspace' already exists" in capsys.readouterr().out
    assert live_shell.list() == {taken: "claude-workspace"}
    assert not received.exists()


def test_a_prompt_launch_needs_a_prompt_and_no_files(tmp_path: Path) -> None:
    for prompt, tasks, work, message in (
        ("   ", None, None, "non-empty prompt"),
        ("hi", tmp_path / "t.md", None, "does not use task or work files"),
        (None, None, None, "supervised launch needs its task and work files"),
    ):
        with pytest.raises(ValueError, match=message):
            claude.launch(
                tmp_path,
                tasks,
                work,
                3600,
                skill_dir=_SKILL_DIR,
                prompt=prompt,
                preset_first_run=False,
            )


@pytest.mark.parametrize(
    "extra",
    [
        ["--prompt", "hello", "--tasks-file", "tasks.md"],
        ["--prompt", "hello", "--work-file", "work.md"],
        ["--prompt", "   "],
    ],
)
def test_claude_prompt_cli_rejects_files_and_blank_prompt(extra: list[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        spawn_claude.main([str(tmp_path), *extra])


def test_antigravity_launch_needs_its_files_or_prompt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a supervised launch needs its task and work files"):
        antigravity.launch(tmp_path, None, None, skill_dir=_SKILL_DIR)


def test_antigravity_prompt_cli_rejects_files_and_blank_prompt(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        spawn_antigravity.main([str(tmp_path), "--prompt", "   "])
