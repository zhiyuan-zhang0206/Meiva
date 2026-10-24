"""First-run and launch safety contracts for the Claude Code launcher."""

from __future__ import annotations

import json
import stat
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from meiva.shell.coding_tools import _claude_checks, _first_run, claude
from meiva.shell.coding_tools.plans.claude_launch import (
    ClaudeLaunchFacts,
    claude_command,
    plan_claude_launch,
)
from meiva.shell.coding_tools.plans.claude_screen import (
    claude_ui_ready,
    failure_text,
    login_marker,
    screen_verdict,
)
from meiva.shell.coding_tools.plans.first_run import (
    CONFIG_PRESET,
    SETTINGS_PRESET,
    plan_preset,
    plan_trust,
)
from meiva.shell.coding_tools.plans.launch_request import LaunchRequest
from meiva.shell.coding_tools.plans.pacing import Pacing
from meiva.shell.coding_tools.tests.fake_tools import CLAUDE_PANEL, site_for
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
_STAMP = "20260920-204500"
_FAST = Pacing(timeout=1.0, poll=0.05, settle=0.05)


def test_claude_command_uses_home_fallback_when_session_path_has_no_claude(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    executable = home / ".local" / "bin" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "launched:%s\\n" "$1"\n')
    executable.chmod(0o755)
    path = tmp_path / "empty-path"
    path.mkdir()

    command = claude_command(tmp_path)
    result = subprocess.run(  # noqa: S603 - executes only the launcher command against a fake CLI
        ["/bin/bash", "-c", command],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "launched:--dangerously-skip-permissions\n"

    path_executable = path / "claude"
    path_executable.write_text('#!/bin/sh\nprintf "path:%s\\n" "$1"\n')
    path_executable.chmod(0o755)
    normal = subprocess.run(  # noqa: S603 - controlled PATH executable verifies precedence
        ["/bin/bash", "-c", command],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert normal.returncode == 0
    assert normal.stdout == "path:--dangerously-skip-permissions\n"


def test_claude_command_exits_when_executable_is_unavailable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "empty-path"
    path.mkdir()

    marker = tmp_path / "missing-claude"
    result = subprocess.run(  # noqa: S603 - isolated shell tests the fail-closed launcher command
        ["/bin/bash", "-c", claude_command(tmp_path, failure_marker=marker)],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 127
    assert "claude executable not found in PATH or $HOME/.local/bin/claude" in result.stderr
    assert marker.read_text() == "claude executable not found\n"


def test_claude_command_refuses_a_signed_out_cli_before_exec(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "bin"
    path.mkdir()
    fake = path / "claude"
    fake.write_text('#!/bin/sh\n[ "$1" = auth ] && exit 1\nprintf "launched\\n"\n')
    fake.chmod(0o755)
    marker = tmp_path / "missing-claude"

    result = subprocess.run(  # noqa: S603 - isolated shell tests the fail-closed launcher command
        ["/bin/bash", "-c", claude_command(tmp_path, failure_marker=marker)],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 126
    assert "launched" not in result.stdout
    assert "claude is not logged in" in result.stderr
    assert login_marker(marker).read_text() == "not logged in\n"


_PANEL_2_1_281 = (
    " ▐▛███▛█   Claude Code v2.1.281\n"
    "▝▜██████▀  Opus 5.5 · Claude Pro\n"
    "  ▝▝ ▝▝    <cwd>\n"
    "  ... (usage note)\n"
    "─────────────────────────────────────────────\n"
    '\u276f\u00a0Try "fix lint errors"\n'
    "─────────────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
_PANEL_2_1_283 = (
    " ▐▛███▜▌   Claude Code v2.1.283\n"
    "▝▜█████▛▘  Opus 5.5 (1M context)\n"
    "  ~/.meiva/workspaces/4\n"
    "────────────\n"
    "\u276f\n"
    "────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)


def _verdict(
    screen: str | None,
    *,
    missing: bool | None = False,
    logged_out: bool | None = False,
    resumed: bool = False,
) -> str:
    return screen_verdict(screen, resumed=resumed, missing_marker=missing, login_marker=logged_out)


@pytest.mark.parametrize("panel", [_PANEL_2_1_281, _PANEL_2_1_283])
def test_ready_accepts_the_recorded_claude_panels(panel: str) -> None:
    assert claude_ui_ready(panel)
    assert _verdict(panel) == "ready"


def test_ready_accepts_other_unicode_spacing_without_a_fixed_suggestion() -> None:
    panel = 'Claude Code v2.1.281\n\u276f\u2009Try "explain this file"\nbypass permissions on\n'
    assert claude_ui_ready(panel)


def test_ready_rejects_non_claude_panel_with_a_composer() -> None:
    panel = 'Other CLI v2.1.281\n\u276f\u00a0Try "fix lint errors"\nbypass permissions on\n'
    assert not claude_ui_ready(panel)


def test_a_command_echo_is_not_a_missing_executable_error(tmp_path: Path) -> None:
    command_echo = "shell $ " + claude_command(tmp_path)
    assert _verdict(command_echo, missing=None) == "waiting"
    assert _verdict(command_echo, missing=False) == "waiting"


def test_without_a_marker_the_missing_executable_line_on_the_screen_is_the_evidence() -> None:
    screen = "error: claude executable not found in PATH or $HOME/.local/bin/claude\n"
    assert _verdict(screen, missing=None, logged_out=None) == "missing"
    assert _verdict(screen, missing=False) == "waiting"


def test_a_private_marker_is_the_evidence_when_the_launch_made_one() -> None:
    assert _verdict(_PANEL_2_1_283, missing=True) == "missing"
    assert _verdict(None, missing=True) == "missing"


def test_an_ended_session_without_a_marker_is_exited() -> None:
    assert _verdict(None) == "exited"
    assert _verdict(None, missing=None, logged_out=None) == "exited"


def test_a_signed_out_cli_is_refused_even_when_it_renders_a_ready_panel() -> None:
    assert _verdict(_PANEL_2_1_283, logged_out=True) == "logged_out"


def test_a_resumed_panel_is_ready_without_the_welcome_banner() -> None:
    resumed_panel = (
        "⏺ takeover active\n"
        "──────────\n"
        "\u276f \n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )
    shell = "host:cl-b user$ exec claude --dangerously-skip-permissions --resume x\n"

    assert _verdict(resumed_panel) == "waiting"
    assert _verdict(resumed_panel, resumed=True) == "ready"
    assert _verdict(shell, resumed=True) == "waiting"


def test_a_long_shell_prompt_and_the_setup_screen_are_not_ready() -> None:
    assert _verdict("exec claude: not found\n" + "bash prompt $ " * 10) == "waiting"
    assert (
        _verdict("Welcome to Claude Code\nChoose the text style that looks best with your terminal")
        == "waiting"
    )


def test_each_failure_says_what_happened() -> None:
    assert "claude executable not found in PATH" in failure_text("missing", 7, settled=False)
    assert "not logged in on this host" in failure_text("logged_out", 7, settled=False)
    assert "exited before its UI appeared" in failure_text("exited", 7, settled=False)
    assert "exited before it became ready" in failure_text("exited", 7, settled=True)


def _session_running(site_shell: ShellSessions, host: SessionHost, home: Path, line: str) -> int:
    session_id, _full = site_shell.create("claude-probe", cwd=home, ttl=120.0, host=host)
    site_shell.send(session_id, line)
    return session_id


def _wait(
    shell: ShellSessions, session_id: int, marker: Path | None, pacing: Pacing = _FAST
) -> None:
    _claude_checks.wait_for_ready(shell, session_id, pacing, failure_marker=marker, resumed=False)


def test_the_wait_returns_once_the_panel_is_up_and_stays_up(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    short_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    panel = short_dir / "panel.txt"
    panel.write_text(CLAUDE_PANEL)
    session_id = _session_running(live_shell, live_host, unit_home, f"sleep 0.3; cat {panel}")

    _wait(live_shell, session_id, None, replace(_FAST, timeout=20.0))

    assert "-> ready (Claude Code UI)" in capsys.readouterr().out


def test_the_wait_times_out_on_a_shell_prompt_and_delivers_nothing(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    session_id = _session_running(live_shell, live_host, unit_home, "echo exec claude: not found")

    with pytest.raises(RuntimeError, match="Claude Code UI did not appear"):
        _wait(live_shell, session_id, short_dir / "missing-claude")


def test_the_wait_reports_a_session_that_exits_before_its_ui_appears(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    session_id = _session_running(live_shell, live_host, unit_home, "exit 0")

    with pytest.raises(RuntimeError, match="exited before its UI appeared"):
        _wait(live_shell, session_id, short_dir / "missing-claude", replace(_FAST, timeout=20.0))


def test_an_exited_session_reports_a_missing_executable_from_the_marker(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    short_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = short_dir / "missing-claude"
    session_id = _session_running(live_shell, live_host, unit_home, f"echo x > {marker}; exit 127")

    with pytest.raises(
        RuntimeError, match=r"claude executable not found in PATH or \$HOME/\.local/bin/claude"
    ):
        _wait(live_shell, session_id, marker, replace(_FAST, timeout=20.0))
    assert (
        "claude executable not found in PATH or $HOME/.local/bin/claude" in capsys.readouterr().out
    )


def test_a_signed_out_claude_fails_fast_instead_of_timing_out(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    # meiva:note
    # A signed-out CLI still renders a ready panel; the launch command's `claude auth status` preflight leaves the marker the wait refuses on.
    marker = short_dir / "missing-claude"
    login_marker(marker).write_text("not logged in\n")
    panel = short_dir / "panel.txt"
    panel.write_text(CLAUDE_PANEL)
    session_id = _session_running(live_shell, live_host, unit_home, f"cat {panel}")

    with pytest.raises(RuntimeError, match="not logged in on this host"):
        _wait(live_shell, session_id, marker, replace(_FAST, timeout=30.0))


def test_a_supervised_launch_does_not_send_the_contract_to_a_shell(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    workspace = short_dir / "ws"
    workspace.mkdir()
    request = LaunchRequest(
        workspace=workspace,
        tasks_file=workspace / "tasks.md",
        work_file=workspace / "work.md",
        ttl_seconds=3600,
        caller_instance=None,
        prompt=None,
        skill_dir=_SKILL_DIR,
        resume=None,
    )
    facts = ClaudeLaunchFacts("s-1", short_dir / "missing-claude", _STAMP, "")
    plan = plan_claude_launch(request, facts)
    # meiva:note
    # The launch line the plan carries asks the machine for `claude`, which a test cannot take away; this stands in for a machine without it: the line writes the marker the real line writes and exits 127.
    missing = replace(
        plan,
        launch_line=f"echo 'claude executable not found' > {plan.failure_marker}; exit 127",
        ready=replace(_FAST, timeout=20.0),
    )

    with pytest.raises(RuntimeError, match="claude executable not found"):
        claude.run_supervised_launch(site_for(live_shell, unit_home, live_host), missing)

    assert live_shell.list() == {}


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _claude_json(home: Path) -> Path:
    return home / ".claude.json"


def test_fresh_home_gets_both_files_preset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _first_run.preset_claude_first_run(home, _STAMP)
    out = capsys.readouterr().out
    assert "+ preset: ~/.claude/settings.json" in out
    assert "+ preset: ~/.claude.json" in out
    assert json.loads(_settings(home).read_text()) == {"skipDangerousModePermissionPrompt": True}
    assert json.loads(_claude_json(home).read_text()) == {"fullscreenUpsellSeenCount": 3}
    assert not list(home.rglob("*.bak-*"))


def test_existing_config_is_preserved_and_backed_up(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text(json.dumps({"env": {"A": "1"}, "model": "opus"}))
    _claude_json(home).write_text(json.dumps({"fullscreenUpsellSeenCount": 1, "userID": "x"}))
    _first_run.preset_claude_first_run(home, _STAMP)
    settings = json.loads(_settings(home).read_text())
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["env"] == {"A": "1"} and settings["model"] == "opus"
    claude_json = json.loads(_claude_json(home).read_text())
    assert claude_json["fullscreenUpsellSeenCount"] == 3 and claude_json["userID"] == "x"
    backups = sorted(p.name for p in home.rglob("*.bak-*"))
    assert len(backups) == 2
    assert all(f".bak-{_STAMP}-" in name for name in backups)


def test_second_call_is_a_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    _first_run.preset_claude_first_run(home, _STAMP)
    capsys.readouterr()
    before = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    _first_run.preset_claude_first_run(home, _STAMP)
    out = capsys.readouterr().out
    assert out.count("(already preset") == 2
    after = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    assert before == after
    assert not list(home.rglob("*.bak-*"))


def test_count_at_or_above_threshold_is_satisfied(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _claude_json(home).write_text(json.dumps({"fullscreenUpsellSeenCount": 5}))
    _first_run.preset_claude_first_run(home, _STAMP)
    assert json.loads(_claude_json(home).read_text())["fullscreenUpsellSeenCount"] == 5


@pytest.mark.parametrize("break_settings", [True, False])
def test_unparsable_file_is_backed_up_and_raises(tmp_path: Path, break_settings: bool) -> None:
    home = tmp_path / "home"
    home.mkdir()
    if break_settings:
        _settings(home).parent.mkdir(parents=True)
        _settings(home).write_text("{not json")
        target = _settings(home)
    else:
        _claude_json(home).write_text("[1,2")
        target = _claude_json(home)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        _first_run.preset_claude_first_run(home, _STAMP)
    assert target.read_text() == ("{not json" if break_settings else "[1,2")
    assert list(home.rglob("*.bak-*"))


# meiva:note
# N1 regression (Meiva #3242 review): unique tmp/backup names under one shared stamp, two threads.
def test_same_second_concurrent_presets_do_not_collide(tmp_path: Path) -> None:
    failures: list[str] = []

    def worker(home: Path, barrier: threading.Barrier) -> None:
        try:
            barrier.wait(timeout=10)
            _first_run.preset_claude_first_run(home, _STAMP)
        except BaseException as exc:
            failures.append(f"{type(exc).__name__}: {exc}")

    for round_no in range(24):
        home = tmp_path / f"home{round_no}"
        (home / ".claude").mkdir(parents=True)
        settings = _settings(home)
        settings.write_text(json.dumps({"env": {"K": "v"}}))
        _claude_json(home).write_text(json.dumps({"userID": "u"}))
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=worker, args=(home, barrier)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not failures, failures
        merged = json.loads(settings.read_text())
        assert merged["env"] == {"K": "v"}
        assert merged["skipDangerousModePermissionPrompt"] is True
        claude_json = json.loads(_claude_json(home).read_text())
        assert claude_json["userID"] == "u"
        assert claude_json["fullscreenUpsellSeenCount"] == 3
        backups = [p.name for p in home.rglob("*.bak-*")]
        assert len(backups) == len(set(backups))
        assert not list(home.rglob("*.tmp"))


def test_existing_file_keeps_its_permissions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text("{}")
    _settings(home).chmod(0o640)
    _first_run.preset_claude_first_run(home, _STAMP)
    assert stat.S_IMODE(_settings(home).stat().st_mode) == 0o640


def test_a_workspace_is_trusted_once_and_the_rest_of_the_file_is_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "ws"
    _claude_json(tmp_path).write_text(
        json.dumps({"userID": "u", "projects": {"/other": {"hasTrustDialogAccepted": True}}})
    )

    _first_run.pretrust(tmp_path, workspace, _STAMP)
    _first_run.pretrust(tmp_path, workspace, _STAMP)

    config = json.loads(_claude_json(tmp_path).read_text())
    assert config["userID"] == "u"
    assert config["projects"]["/other"] == {"hasTrustDialogAccepted": True}
    assert config["projects"][workspace.as_posix()] == {"hasTrustDialogAccepted": True}
    out = capsys.readouterr().out
    assert out.count("+ trusted:") == 1 and out.count("(already trusted:") == 1
    assert not list(tmp_path.glob("*.bak-*"))


def test_trusting_a_workspace_creates_the_file(tmp_path: Path) -> None:
    _first_run.pretrust(tmp_path, tmp_path / "ws", _STAMP)

    config = json.loads(_claude_json(tmp_path).read_text())
    assert config == {"projects": {(tmp_path / "ws").as_posix(): {"hasTrustDialogAccepted": True}}}


def test_an_unparsable_config_is_never_overwritten_by_a_trust(tmp_path: Path) -> None:
    _claude_json(tmp_path).write_text("{not json")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        _first_run.pretrust(tmp_path, tmp_path / "ws", _STAMP)

    assert _claude_json(tmp_path).read_text() == "{not json"
    assert list(tmp_path.glob(".claude.json.bak-*"))


def test_the_preset_decisions_follow_the_files_text() -> None:
    label = "~/.claude.json"
    assert plan_preset(None, CONFIG_PRESET, label).action == "write"
    assert plan_preset("", CONFIG_PRESET, label).data == {"fullscreenUpsellSeenCount": 3}
    assert plan_preset('{"fullscreenUpsellSeenCount": 4}', CONFIG_PRESET, label).action == "keep"
    assert plan_preset('{"fullscreenUpsellSeenCount": 2}', CONFIG_PRESET, label).action == "write"
    assert plan_preset("[1]", CONFIG_PRESET, label).action == "refuse"
    assert plan_preset("{oops", SETTINGS_PRESET, label).action == "refuse"
    assert plan_preset(
        '{"skipDangerousModePermissionPrompt": "yes"}', SETTINGS_PRESET, label
    ).data == {"skipDangerousModePermissionPrompt": True}


def test_the_trust_decision_keeps_an_already_trusted_workspace_alone() -> None:
    raw = json.dumps({"projects": {"/ws": {"hasTrustDialogAccepted": True, "x": 1}}})
    assert plan_trust(raw, "/ws", "~/.claude.json").action == "keep"
    assert plan_trust(None, "/ws", "~/.claude.json").data == {
        "projects": {"/ws": {"hasTrustDialogAccepted": True}}
    }
