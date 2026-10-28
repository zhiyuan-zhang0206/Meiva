from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, cast

import pytest

from base.sessions import coding_session_owner
from base.sessions import tool_capabilities as caps
from base.sessions.backend import get_shell_backend
from meiva.shell.coding_tools import _common

NOW = dt.datetime(2026, 10, 18, 15, 0, tzinfo=dt.UTC)


def _binary(tmp_path: Path) -> Path:
    path = tmp_path / "fake-claude"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _spec(binary: Path) -> caps.ToolSpec:
    return caps.ToolSpec(
        tool="fake-claude",
        candidates=(str(binary),),
        features=(
            caps.Feature(
                name="stream_json_out",
                marker="--output-format",
                fallback="pty",
                min_version=(2, 1, 283),
            ),
            caps.Feature(name="resume", marker="--resume", fallback="fresh-session"),
        ),
    )


def _runner(
    *, version: str = "2.1.284 (Claude Code)", help_text: str = "--output-format --resume"
) -> tuple[caps.Runner, list[list[str]]]:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if "--version" in argv:
            return 0, version
        return 0, help_text

    return run, calls


def _probe(**overrides: object) -> caps.Probe:
    fields: dict[str, object] = {
        "tool": "fake-claude",
        "path": "/usr/local/bin/fake-claude",
        "version": (2, 1, 284),
        "version_text": "2.1.284 (Claude Code)",
        "flags": {"stream_json_out": True, "resume": True},
        "unknown_reason": None,
        "probed_at": NOW,
    }
    fields.update(overrides)
    return caps.Probe(**cast("dict[str, Any]", fields))


def test_parse_version_reads_both_banners_and_rejects_noise() -> None:
    assert caps.parse_version("2.1.284 (Claude Code)") == (2, 1, 284)
    assert caps.parse_version("codex-cli 0.163.0") == (0, 163, 0)
    assert caps.parse_version("nightly-2026-10") is None


def test_probe_reads_the_version_and_the_declared_flags(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    run, _calls = _runner()

    probe = caps.probe_tool(
        _spec(binary), run=run, store=tmp_path / "probes.json", now=NOW, user_home=tmp_path
    )

    assert probe.path == str(binary)
    assert probe.version == (2, 1, 284)
    assert probe.flags == {"stream_json_out": True, "resume": True}
    assert probe.unknown_reason is None


def test_a_binary_outside_path_is_unknown_not_absent(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "does-not-exist")
    run, calls = _runner()

    probe = caps.probe_tool(
        spec, run=run, store=tmp_path / "probes.json", now=NOW, user_home=tmp_path
    )

    assert probe.unknown_reason is not None
    assert "not found" in probe.unknown_reason
    assert probe.version is None
    assert calls == []


def test_a_cached_probe_costs_no_second_run(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    spec = _spec(binary)
    store = tmp_path / "probes.json"
    run, calls = _runner()
    caps.probe_tool(spec, run=run, store=store, now=NOW, user_home=tmp_path)
    first = len(calls)

    caps.probe_tool(spec, run=run, store=store, now=NOW, user_home=tmp_path)

    assert len(calls) == first


def test_cache_freshness_is_judged_at_the_supplied_instant(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    spec = _spec(binary)
    store = tmp_path / "probes.json"
    run, calls = _runner()
    caps.probe_tool(spec, run=run, store=store, now=NOW, user_home=tmp_path)
    first = len(calls)

    later = NOW + dt.timedelta(seconds=caps.PROBE_TTL_SECONDS + 1)
    caps.probe_tool(spec, run=run, store=store, now=later, user_home=tmp_path)

    assert len(calls) > first


def test_an_upgraded_binary_is_probed_again(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    spec = _spec(binary)
    store = tmp_path / "probes.json"
    run, calls = _runner()
    caps.probe_tool(spec, run=run, store=store, now=NOW, user_home=tmp_path)
    first = len(calls)
    os.utime(binary, (1, 1))

    caps.probe_tool(spec, run=run, store=store, now=NOW, user_home=tmp_path)

    assert len(calls) > first


def test_an_unparseable_version_is_unknown_but_flags_survive(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    run, _calls = _runner(version="dev build")

    probe = caps.probe_tool(
        _spec(binary), run=run, store=tmp_path / "probes.json", now=NOW, user_home=tmp_path
    )

    assert probe.unknown_reason is not None
    assert probe.flags == {"stream_json_out": True, "resume": True}


def test_states_follow_version_and_flags() -> None:
    spec = _spec(Path("/usr/local/bin/fake-claude"))
    stream, resume = spec.features

    assert _probe().state(stream) == "verified"
    assert _probe().state(resume) == "verified"
    assert _probe(version=(2, 1, 100)).state(stream) == "present_untested"
    assert _probe(flags={"resume": True}).state(stream) == "below_floor"
    assert _probe(unknown_reason="not found").state(stream) == "unknown"


def test_auto_degrades_to_pty_and_says_why() -> None:
    spec = _spec(Path("/usr/local/bin/fake-claude"))
    probe = _probe(version=(2, 1, 100), version_text="2.1.100")

    decision = caps.decide(spec, probe=probe)

    assert decision.refused is False
    assert decision.mode == "pty"
    assert any("present_untested" in reason for reason in decision.reasons)


def test_an_explicit_unimplemented_mode_is_refused() -> None:
    spec = _spec(Path("/usr/local/bin/fake-claude"))

    decision = caps.decide(spec, requested="stream-json", probe=_probe())

    assert decision.refused is True
    assert decision.mode == "stream-json"


def test_force_keeps_one_path_and_says_so() -> None:
    spec = _spec(Path("/usr/local/bin/fake-claude"))
    probe = _probe(version=(2, 1, 100), version_text="2.1.100")

    decision = caps.decide(spec, force=True, probe=probe)

    assert decision.refused is False
    assert any("forced" in reason for reason in decision.reasons)


def test_an_unknown_probe_degrades_instead_of_refusing() -> None:
    spec = _spec(Path("/usr/local/bin/fake-claude"))

    decision = caps.decide(spec, probe=_probe(unknown_reason="not found", version=None))

    assert decision.refused is False
    assert decision.mode == "pty"
    assert any("unknown" in reason for reason in decision.reasons)


def test_the_report_line_names_version_path_features_and_decision() -> None:
    spec = _spec(Path("/usr/local/bin/fake-claude"))
    probe = _probe()
    decision = caps.decide(spec, probe=probe)

    line = caps.render(spec.tool, decision, probe)

    assert line.startswith("harness: fake-claude=2.1.284 [")
    assert "features=[resume,stream_json_out]" in line
    assert "decision=pty" in line


def test_status_previews_the_requested_mode_and_returns_its_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = coding_session_owner.canonical_key(
        tmp_path / "workspace", tool="claude", cluster=tmp_path / "cluster"
    )
    line = (
        "harness: claude=2.1.284 [/usr/local/bin/claude] features=[] decision=stream-json (refused)"
    )

    code = _common.status(key, capability=(line, 2))

    assert code == 2
    assert line in capsys.readouterr().out


def test_status_still_reports_the_path_a_launch_would_take(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = coding_session_owner.canonical_key(
        tmp_path / "workspace", tool="claude", cluster=tmp_path / "cluster"
    )

    code = _common.status(key, capability=("harness: claude decision=pty", 0))

    output = capsys.readouterr().out
    assert code == 0
    assert "status=inactive" in output
    assert "harness: claude decision=pty" in output


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_an_explicit_unimplemented_mode_is_refused_whatever_the_machine_has(
    unit_home: Path, tool: str
) -> None:
    # meiva:note
    # The probe runs the machine's own CLI, so the line differs per host; the decision to refuse a mode this build does not implement does not.
    line, code = _common.capability_report(tool, mode="stream-json", force=True)

    assert code == 2
    assert f"harness: {tool}=" in line
    assert "decision=stream-json" in line


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_auto_degrades_to_the_one_implemented_path(unit_home: Path, tool: str) -> None:
    line, code = _common.capability_report(tool)

    assert code == 0
    assert "decision=pty" in line


def test_a_tool_with_no_declared_capabilities_reports_nothing(unit_home: Path) -> None:
    assert _common.capability_report("antigravity") == ("", 0)


# meiva:note
# The shell sessions of the test cluster: no PTY service listens there, so none is live and none is stopped.
def _cluster_sessions(tmp_path: Path) -> coding_session_owner.CodingSessions:
    return get_shell_backend(home=tmp_path / "cluster")


def _launching(tmp_path: Path, tool: str) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(workspace, tool=tool, cluster=tmp_path / "cluster")
    return _common.new_generation(
        key,
        owner_agent_id=41,
        tasks_file=None,
        work_file=None,
        ttl_seconds=3600,
        sessions=_cluster_sessions(tmp_path),
    )


@pytest.mark.parametrize(
    ("tool", "present", "absent"),
    [
        ("claude", "state_dir=", "supervisor_session_id="),
        ("codex", "owner_agent_id=41", "state_dir="),
    ],
)
def test_status_lists_every_generation_recorded_for_the_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tool: str, present: str, absent: str
) -> None:
    owner = _launching(tmp_path, tool)

    assert _common.status(owner.key, capability=("", 0)) == 0

    out = capsys.readouterr().out
    assert "status=launching" in out
    assert f"generation={owner.generation}" in out
    assert present in out
    assert absent not in out


def test_cancel_terminalizes_exactly_the_named_generation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = _launching(tmp_path, "codex")
    assert owner.generation is not None

    sessions = _cluster_sessions(tmp_path)
    assert _common.cancel(owner.key, owner.generation, sessions=sessions) == 0

    assert "status=terminal" in capsys.readouterr().out
    assert _common.cancel(owner.key, "11111111-2222-3333-4444-555555555555", sessions=sessions) == 1
    assert "cancel refused" in capsys.readouterr().err
