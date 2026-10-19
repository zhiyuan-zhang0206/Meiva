from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, cast

import pytest

from base.sessions import coding_session_owner
from base.sessions import tool_capabilities as caps
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

    probe = caps.probe_tool(_spec(binary), run=run, store=tmp_path / "probes.json", now=NOW)

    assert probe.path == str(binary)
    assert probe.version == (2, 1, 284)
    assert probe.flags == {"stream_json_out": True, "resume": True}
    assert probe.unknown_reason is None


def test_a_binary_outside_path_is_unknown_not_absent(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "does-not-exist")
    run, calls = _runner()

    probe = caps.probe_tool(spec, run=run, store=tmp_path / "probes.json", now=NOW)

    assert probe.unknown_reason is not None
    assert "not found" in probe.unknown_reason
    assert probe.version is None
    assert calls == []


def test_a_cached_probe_costs_no_second_run(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    spec = _spec(binary)
    store = tmp_path / "probes.json"
    run, calls = _runner()
    caps.probe_tool(spec, run=run, store=store, now=NOW)
    first = len(calls)

    caps.probe_tool(spec, run=run, store=store, now=NOW)

    assert len(calls) == first


def test_cache_freshness_is_judged_at_the_supplied_instant(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    spec = _spec(binary)
    store = tmp_path / "probes.json"
    run, calls = _runner()
    caps.probe_tool(spec, run=run, store=store, now=NOW)
    first = len(calls)

    later = NOW + dt.timedelta(seconds=caps.PROBE_TTL_SECONDS + 1)
    caps.probe_tool(spec, run=run, store=store, now=later)

    assert len(calls) > first


def test_an_upgraded_binary_is_probed_again(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    spec = _spec(binary)
    store = tmp_path / "probes.json"
    run, calls = _runner()
    caps.probe_tool(spec, run=run, store=store, now=NOW)
    first = len(calls)
    os.utime(binary, (1, 1))

    caps.probe_tool(spec, run=run, store=store, now=NOW)

    assert len(calls) > first


def test_an_unparseable_version_is_unknown_but_flags_survive(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    run, _calls = _runner(version="dev build")

    probe = caps.probe_tool(_spec(binary), run=run, store=tmp_path / "probes.json", now=NOW)

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
    reported: list[tuple[str, str, bool]] = []
    line = (
        "harness: claude=2.1.284 [/usr/local/bin/claude] features=[] decision=stream-json (refused)"
    )

    def report(tool: str, *, mode: str = "auto", force: bool = False) -> tuple[str, int]:
        reported.append((tool, mode, force))
        return line, 2

    code = _common.status(key, lambda _owner: None, mode="stream-json", force=True, report=report)

    assert code == 2
    assert reported == [("claude", "stream-json", True)]
    assert line in capsys.readouterr().out


def test_status_still_reports_the_path_a_launch_would_take(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = coding_session_owner.canonical_key(
        tmp_path / "workspace", tool="claude", cluster=tmp_path / "cluster"
    )

    def report(tool: str, *, mode: str = "auto", force: bool = False) -> tuple[str, int]:
        return f"harness: {tool} decision=pty", 0

    code = _common.status(key, lambda _owner: None, report=report)

    output = capsys.readouterr().out
    assert code == 0
    assert "status=inactive" in output
    assert "harness: claude decision=pty" in output
