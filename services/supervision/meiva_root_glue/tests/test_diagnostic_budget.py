# meiva:note
# How a host check spends its observation budget, and what it reports without a verdict. The venv
# and brew-pin legs used to carry their own five-second literal while the check that observed them
# allowed twenty, so a host busy enough to slow a subcommand reported itself as a broken
# environment (2026-10-27, task #5656). These cover the two halves of the repair: the budget
# reaches the subcommands, and a subcommand that outlives its share raises no finding.

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import cast

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.daemon.health import DaemonProbe, ProbeVerdict
from base.host.proc import run_bounded
from base.log.tests.log_records import LogRecords
from services.supervision.meiva_root_glue import diagnostic_probes as probes
from services.supervision.meiva_root_glue.diagnostics import Diagnostic, DiagnosticMonitor
from services.supervision.meiva_root_glue.tests.slices import (
    runner_that_hangs_on,
    system_clock,
)


# meiva:note
# A checkout whose virtualenv interpreter is a stand-in that reports readable imports.
def _fake_checkout(root: Path) -> Path:
    interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o700)
    return root


# meiva:note
# The environment that resolves `uv` to a stand-in whose body is `body`. The stand-in's own shell
# needs no PATH, but the sleeps it runs do: a fixed system directory, never the ambient PATH.
def _fake_uv(root: Path, body: str) -> dict[str, str]:
    uv = root / "uv"
    uv.write_text(f"#!/bin/sh\n{body}\n")
    uv.chmod(0o700)
    return {"PATH": f"{root}{os.pathsep}/bin{os.pathsep}/usr/bin"}


# meiva:note
# The deadline reaches the subcommands: a leg slower than the retired five-second literal, and well
# inside the check's own deadline, is a healthy reading.
def test_a_venv_leg_inside_the_observation_deadline_is_not_a_failure(tmp_path: Path) -> None:
    environment = _fake_uv(_fake_checkout(tmp_path), "sleep 1")

    probe = probes.venv(
        environment=environment,
        run=run_bounded,
        clock=system_clock(),
        deadline_s=20.0,
        source_root=tmp_path,
    )

    assert probe.alive


# meiva:note
# Nothing was read by the hung leg, so nothing may be concluded from it — and the leg after it still
# ran, because each leg may spend only an even share of what the legs before it left (2026-10-27).
# The hung leg uses its whole share on the test's clock, so the import leg is given the other half
# of the deadline, and the deadline is far longer than the real import leg takes.
def test_a_venv_leg_that_outlives_the_deadline_is_inconclusive_not_down(tmp_path: Path) -> None:
    environment = _fake_uv(_fake_checkout(tmp_path), "exit 0")
    timers = ManualTimers()
    run, allowances = runner_that_hangs_on(str(tmp_path / "uv"), timers)

    probe = probes.venv(
        environment=environment,
        run=run,
        clock=Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=timers),
        deadline_s=2000.0,
        source_root=tmp_path,
    )

    assert probe.verdict is ProbeVerdict.INCONCLUSIVE
    assert "no verdict within the 2000s observation deadline" in probe.detail
    assert (
        "uv pip check did not finish within its share of the observation deadline" in probe.detail
    )
    assert "import smoke" not in probe.detail
    assert allowances == [1000.0, 1000.0]


# meiva:note
# A leg the host could not even start is the busy-host shape this check has to survive: the binary
# resolves but does not exec (EAGAIN/ENOMEM under load, ENOENT for a moved interpreter), which is
# "never read", so the check answers INCONCLUSIVE and the run of those samples — three rounds in
# three minutes — is what `meiva-ops-root-diagnostic-no-verdict` reports (2026-10-27, task #5656).
def test_a_venv_leg_the_host_could_not_start_is_no_verdict(tmp_path: Path) -> None:
    environment = _fake_uv(_fake_checkout(tmp_path), "exit 0")
    unstartable = tmp_path / "uv"
    unstartable.write_text("#!/nonexistent/interpreter\nexit 0\n")
    unstartable.chmod(0o700)

    probe = probes.venv(
        environment=environment,
        run=run_bounded,
        clock=system_clock(),
        deadline_s=20.0,
        source_root=tmp_path,
    )

    assert probe.verdict is ProbeVerdict.INCONCLUSIVE
    assert "uv pip check could not start" in probe.detail


# meiva:note
# A no-verdict sample neither resolves nor multiplies the episode it belongs to: it extends the
# streak, it never reaches the alert reporter, and it reports at INFO. The rows are what
# `meiva-ops-root-diagnostic-no-verdict` counts, so a host that stays too busy to measure for three
# rounds is still reported — the failure rules stay quiet, the run itself does not (2026-10-27).
async def test_consecutive_no_verdict_rounds_are_reported_without_a_verdict(
    loguru_records: LogRecords,
) -> None:
    failed = DaemonProbe.down("approved Homebrew formulae are unpinned: redis")
    current: DaemonProbe = failed
    reported: list[DaemonProbe] = []
    monitor = DiagnosticMonitor(
        [Diagnostic("brew-pin", lambda: current, interval_s=0.001, report=reported.append)],
        clock=system_clock(),
    )
    await monitor.run_round()
    await asyncio.sleep(0.002)
    busy = (
        DaemonProbe.inconclusive(
            "brew list --pinned did not finish within the 18s observation deadline"
        ),
        DaemonProbe.unavailable("brew list --pinned could not start: Permission denied"),
        DaemonProbe.inconclusive("brew list --formula was never started"),
    )
    for verdict in busy:
        current = verdict
        await monitor.run_round()
        await asyncio.sleep(0.002)

    rows = loguru_records.events("root_diagnostic")
    assert [row["extra"]["verdict"] for row in rows] == [
        "down",
        "inconclusive",
        "unavailable",
        "inconclusive",
    ]
    assert [row["extra"]["consecutive_failures"] for row in rows] == [1, 2, 3, 4]
    assert [row["level"].name for row in rows] == ["WARNING", "INFO", "INFO", "INFO"]
    assert reported == [failed]
    snapshot = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
    assert snapshot["diagnostic:brew-pin"]["last_verdict"] == "inconclusive"
