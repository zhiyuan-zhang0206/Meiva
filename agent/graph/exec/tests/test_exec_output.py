"""agent/graph/exec/output.py's wrap_code_output pure function test.

`wrap_code_output` is the envelope fed to LLM — agent relies on "Code execution output" prefix to identify subprocess feedback. Anchor format; changing one side will red the other side test.

`output` single param = subprocess stdout + stderr merged stream (`stderr=STDOUT`, equivalent shell 2>&1), preserves real timing order; no longer separate stdout / stderr.
return code not in envelope — framework internal signal (42 / 43) goes via ToolMessage metadata.

Timestamp is supplied by the composition caller; tests pass fixed rendered text.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.graph.exec.node import dispatch_exec_result
from agent.graph.exec.output import OutputLimits, format_elapsed, wrap_code_output
from agent.graph.exec.tests.event_capture import idle_publisher
from base.agents.messages.inbound import InterruptReason

_TIMESTAMP = "[2026-05-06 14:32:05]"
_TS = _TIMESTAMP  # shorthand


def test_wrap_code_output_structured(limits: OutputLimits, output_now: datetime):
    """code_output envelope contains output, **does not** contain exit code."""
    out = wrap_code_output(
        "hello\nwarn\n",
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "Code execution output" in out
    assert _TS in out
    assert "hello" in out and "warn" in out
    assert "[exit" not in out


def test_wrap_code_output_cancelled_marker(limits: OutputLimits, output_now: datetime):
    """When cancelled, carries [cancelled by user] marker."""
    out = wrap_code_output(
        "part\n",
        cancelled=True,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[cancelled by user]" in out
    assert _TS in out
    assert "part" in out


@pytest.mark.parametrize(
    ("reason", "marker"),
    [
        (InterruptReason.USER, "[cancelled by user]"),
        (InterruptReason.SYSTEM, "[cancelled by system]"),
        (InterruptReason.RESTART, "[interrupted by restart]"),
    ],
)
def test_wrap_code_output_names_what_ended_the_run(
    limits: OutputLimits, output_now: datetime, reason: InterruptReason, marker: str
):
    out = wrap_code_output(
        "part\n",
        cancelled=True,
        cancel_reason=reason,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    header = out.splitlines()[0]
    assert marker in header
    assert sum(m in header for m in ("[cancelled by", "[interrupted by")) == 1
    assert "part" in out


def test_wrap_code_output_appends_trailing_newline_when_missing(
    limits: OutputLimits, output_now: datetime
):
    """When output lacks trailing \\n, envelope still normalizes to single line — nothing to join afterwards, mainly for more stable frontend rendering."""
    out = wrap_code_output(
        "partial output",
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "partial output" in out
    assert _TS in out


def test_wrap_code_output_no_stderr_marker_in_envelope(limits: OutputLimits, output_now: datetime):
    """stdout/stderr already merged, envelope should not have '--- stderr ---' separator marker — keeping it directly contradicts 'merged stream' design."""
    out = wrap_code_output(
        "hello\nTraceback (most recent call last):\n  ...\n",
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "--- stderr ---" not in out


def test_wrap_code_output_no_output_marker_when_empty(limits: OutputLimits, output_now: datetime):
    """When output is empty, show '(no output)' — explicitly tell agent code ran but produced no output (avoid empty envelope making LLM mistakenly think exec failed/didn't run)."""
    out = wrap_code_output(
        "",
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "Code execution output" in out
    assert "(no output)" in out
    assert _TS in out


def test_wrap_code_output_no_output_marker_omitted_when_output_present(
    limits: OutputLimits, output_now: datetime
):
    """When output present, don't add '(no output)' marker — marker is only fallback for empty envelope."""
    out = wrap_code_output(
        "real output\n",
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "(no output)" not in out


def test_wrap_code_output_no_output_marker_with_cancelled(
    limits: OutputLimits, output_now: datetime
):
    """Under cancel path, even if output empty, add '(no output)' marker — agent sees [cancelled by user] + (no output) knows it was interrupted and produced nothing."""
    out = wrap_code_output(
        "",
        cancelled=True,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[cancelled by user]" in out
    assert "(no output)" in out
    assert _TS in out


def test_wrap_code_output_header_body_double_newline_split(
    limits: OutputLimits, output_now: datetime
):
    """Between header and body use \\n\\n separator (same contract as wrap_inbound, frontend splitEnvelope splits header/body by this)."""
    out = wrap_code_output(
        "body line\n",
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    # header must be followed by double \n
    assert f"Code execution output after running for 1.0s {_TS}:\n\nbody line" in out


def test_wrap_code_output_truncates_keeps_both_ends_and_writes_file(
    limits: OutputLimits,
    output_now: datetime,
):
    """Exceeds exec_output_max_chars → keep head + tail, cut middle, full output written to tmp file and path reported.
    Head carries help() overview / overview, tail carries error / result, both ends must survive."""
    limit = 1000
    head_marker = "HEAD_START"
    tail_marker = "TAIL_END"
    big = head_marker + ("X" * 5000) + ("M" * (limit * 3)) + ("Y" * 5000) + tail_marker
    out = wrap_code_output(
        big,
        limits=replace(limits, max_chars=limit),
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text

    assert head_marker in out, "head must be preserved (help overview at start)"
    assert tail_marker in out, "tail must be preserved (error / result usually at end)"
    assert "M" * (limit * 3) not in out, "middle should be cut"
    assert "output truncated" in out and "omitted" in out, (
        "marker indicates truncation + how much omitted"
    )

    # path is reported, and the file holds the complete output
    files = list(limits.archive_dir.glob("exec_*.txt"))
    assert len(files) == 1
    assert str(files[0]) in out, "complete output path must be reported to agent"
    assert files[0].read_text(encoding="utf-8") == big


def test_wrap_code_output_overflow_files_pruned_to_keep_limit(
    limits: OutputLimits,
    output_now: datetime,
):
    """Same agent repeatedly overflows keeps only recent N, prevents workspace infinite pile-up."""
    for second in range(22):
        wrap_code_output(
            "Z" * 2000,
            limits=replace(limits, max_chars=100),
            now=output_now + timedelta(seconds=second),
            timestamp=_TS,
            elapsed_seconds=1.0,
        )
    files = list(limits.archive_dir.glob("exec_*.txt"))
    assert len(files) == 20, "only keep the most recent 20 overflow archives"


def test_wrap_code_output_no_truncation_when_under_limit(
    limits: OutputLimits, output_now: datetime
):
    """≤ exec_output_max_chars returns directly unchanged, no marker added."""
    just_under = "y" * (30_000 - 100)
    out = wrap_code_output(
        just_under,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "output truncated" not in out
    assert just_under in out


def test_wrap_code_output_timed_out_marker(limits: OutputLimits, output_now: datetime):
    """When timed out, carries [timeout after Ns] marker (N from settings.sandbox.exec_timeout_seconds), no cancel marker."""
    out = wrap_code_output(
        "part\n",
        timed_out=True,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[timeout after 60s]" in out
    assert "[cancelled by user]" not in out
    assert "part" in out


def test_wrap_code_output_timed_out_carries_strategy_hint(
    limits: OutputLimits, output_now: datetime
):
    """timeout envelope is agent's only clue to change strategy — hint must name long-task primitive; non-timeout path no hint."""
    out = wrap_code_output(
        "part\n",
        timed_out=True,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "run_background" in out
    assert "meiva.watcher.launch" in out
    assert (
        "run_background"
        not in wrap_code_output(
            "part\n",
            limits=limits,
            now=output_now,
            timestamp=_TS,
            elapsed_seconds=1.0,
        ).text
    )
    assert (
        "run_background"
        not in wrap_code_output(
            "part\n",
            cancelled=True,
            limits=limits,
            now=output_now,
            timestamp=_TS,
            elapsed_seconds=1.0,
        ).text
    )


def test_wrap_code_output_timed_out_empty(limits: OutputLimits, output_now: datetime):
    """Under timeout path, even output empty, adds '(no output)' marker, hint still present."""
    out = wrap_code_output(
        "",
        timed_out=True,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[timeout after 60s]" in out
    assert "(no output)" in out
    assert "run_background" in out
    assert "[cancelled by user]" not in out


def test_wrap_code_output_cancelled_wins_over_timed_out_marker(
    limits: OutputLimits, output_now: datetime
):
    """When both cancel and timed_out True, cancel marker shows (priority).
    However wrap_code_output only receives one bool — caller (exec_node) guarantees mutual exclusion.
    Here verify when cancelled=True, no timeout marker appears."""
    out = wrap_code_output(
        "data\n",
        cancelled=True,
        limits=limits,
        now=output_now,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[cancelled by user]" in out
    assert "[timeout after 60s]" not in out


def test_wrap_code_output_no_timestamp_when_disabled(limits: OutputLimits, output_now: datetime):
    """settings.general.message_timestamps=False → header drops the timestamp and the
    space before the colon, keeping any marker intact."""
    assert wrap_code_output(
        "hello\n",
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text.startswith("Code execution output after running for 1.0s:\n\n")
    assert (
        _TS
        not in wrap_code_output(
            "hello\n",
            limits=limits,
            now=output_now,
            elapsed_seconds=1.0,
        ).text
    )
    # marker survives, still no timestamp / stray space
    out = wrap_code_output(
        "part\n",
        cancelled=True,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert "Code execution output after running for 1.0s [cancelled by user]:\n\n" in out
    assert _TS not in out


# ── P0 #2100: a crashed exec with empty output must never read as "(no output)" ──


def _boot_crash(exc: Exception, code_reached: bool | None) -> object:
    from agent.graph.exec._result import ExecCrashed

    return ExecCrashed(output="", exc=exc, full_traceback=None, code_reached=code_reached)


def _dispatch(
    result: object, limits: OutputLimits, now: datetime, agent_id: int = 7
) -> tuple[bool, str]:
    plan, envelope = dispatch_exec_result(
        result,  # type: ignore[arg-type]
        idle_publisher(),
        agent_id,
        elapsed_seconds=1.0,
        timestamp=None,
        referenced_messages=(),
        limits=limits,
        now=now,
    )
    return plan.halted, envelope.text


def test_dispatch_boot_crash_reports_not_executed(
    limits: OutputLimits, output_now: datetime
) -> None:
    """The outage shape: child crashed on bootstrap fetch, stdout empty. The
    agent must read "code was NOT executed", never "(no output)"."""
    from agent.graph.exec._result import ExecChildError

    exc = ExecChildError("BootstrapFetchError", "could not fetch cluster config", "tb")
    halted, text = _dispatch(_boot_crash(exc, False), limits, output_now)
    assert halted is False
    assert "Code execution output" in text
    assert "the code was NOT executed" in text
    assert "BootstrapFetchError: could not fetch cluster config" in text
    assert "(no output)" not in text


def test_dispatch_crash_with_output_keeps_the_output(
    limits: OutputLimits, output_now: datetime
) -> None:
    """A crash WITH stdout keeps the agent-facing traceback — no marker added."""
    from agent.graph.exec._result import ExecChildError, ExecCrashed

    result = ExecCrashed(
        output="Traceback (most recent call last):\n  boom\n",
        exc=ExecChildError("ValueError", "boom", None),
        code_reached=True,
    )
    halted, text = _dispatch(result, limits, output_now)
    assert halted is False
    assert "Traceback (most recent call last)" in text
    assert "(no output)" not in text


def test_dispatch_boot_crash_emits_the_boot_failed_event(
    loguru_records: list[dict[str, Any]], limits: OutputLimits, output_now: datetime
) -> None:
    """P2 #2102: a boot-phase failure (code_reached=False) emits the WARNING
    exec_child_boot_failed event the alert rule reads; the unknown case does not."""
    from agent.graph.exec._result import ExecChildError

    boot_exc = ExecChildError("BootstrapFetchError", "down", None)
    _dispatch(_boot_crash(boot_exc, False), limits, output_now, agent_id=42)
    boot = [r for r in loguru_records if r["extra"].get("event") == "exec_child_boot_failed"]
    assert len(boot) == 1
    assert boot[0]["level"].name == "WARNING"
    assert boot[0]["extra"]["agent_id"] == 42
    assert boot[0]["extra"]["exc_type"] == "BootstrapFetchError"
    assert boot[0]["extra"]["exc_msg"] == "down"

    loguru_records.clear()
    unknown_exc = ExecChildError("exec_subprocess_aborted", "gone", None)
    _dispatch(_boot_crash(unknown_exc, None), limits, output_now, agent_id=42)
    assert not [r for r in loguru_records if r["extra"].get("event") == "exec_child_boot_failed"]


def test_archive_stamp_is_the_callers_instant_in_the_callers_zone(
    limits: OutputLimits,
) -> None:
    now = datetime(2026, 10, 9, 9, 2, 3, 456789, tzinfo=ZoneInfo("Asia/Shanghai"))
    wrapped = wrap_code_output(
        "x" * 200,
        limits=replace(limits, max_chars=100),
        now=now,
        timestamp="[caller supplied timestamp]",
        elapsed_seconds=1.0,
    ).text
    assert wrapped.startswith(
        "Code execution output after running for 1.0s [caller supplied timestamp]:\n\n"
    )
    files = list(limits.archive_dir.glob("exec_20261009_090203_456789.txt"))
    assert len(files) == 1
    assert str(files[0]) in wrapped
    assert files[0].read_text() == "x" * 200


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.0, "0ms"),
        (0.85, "850ms"),
        (4.24, "4.2s"),
        (53.4, "53s"),
        (60.0, "1min 0s"),
        (113.0, "1min 53s"),
        (1200.0, "20min 0s"),
        (3723.0, "1h 2min 3s"),
    ],
)
def test_format_elapsed(seconds: float, text: str):
    assert format_elapsed(seconds) == text


@pytest.mark.parametrize(
    ("kwargs", "header"),
    [
        ({}, "Code execution output after running for 1min 53s [ts]:"),
        (
            {"cancelled": True},
            "Code execution output after running for 1min 53s [cancelled by user] [ts]:",
        ),
        (
            {"timed_out": True},
            "Code execution output after running for 1min 53s [timeout after 60s] [ts]:",
        ),
        ({"timestamp": None}, "Code execution output after running for 1min 53s:"),
    ],
)
def test_header_carries_elapsed_in_every_ending(
    kwargs: dict[str, object], header: str, limits: OutputLimits, output_now: datetime
):
    params: dict[str, object] = {"timestamp": "[ts]", **kwargs}
    out = wrap_code_output(
        "x\n",
        limits=limits,
        now=output_now,
        elapsed_seconds=113.0,
        **params,  # type: ignore[arg-type]
    ).text
    assert out.startswith(header + "\n\n")


@pytest.mark.parametrize(
    ("kind", "status", "halted"),
    [
        ("done", "completed", False),
        ("timed_out", "timed_out", False),
        ("cancelled", "cancelled", True),
        ("crashed", "failed", False),
    ],
)
def test_dispatch_reports_status_and_body_start(
    kind: str, status: str, halted: bool, limits: OutputLimits, output_now: datetime
) -> None:
    from agent.graph.exec._result import ExecCancelled, ExecCrashed, ExecDone, ExecTimedOut

    results: dict[str, object] = {
        "done": ExecDone(output="o\n"),
        "timed_out": ExecTimedOut(output="o\n"),
        "cancelled": ExecCancelled(output="o\n"),
        "crashed": ExecCrashed(output="o\n", exc=ValueError("x"), full_traceback=None),
    }
    plan, envelope = dispatch_exec_result(
        results[kind],  # type: ignore[arg-type]
        idle_publisher(),
        7,
        elapsed_seconds=113.0,
        timestamp=None,
        referenced_messages=(),
        limits=limits,
        now=output_now,
    )
    assert (plan.halted, plan.status.value) == (halted, status)
    assert envelope.text[envelope.body_start :].startswith("o\n")
    assert envelope.text.startswith("Code execution output after running for 1min 53s")
