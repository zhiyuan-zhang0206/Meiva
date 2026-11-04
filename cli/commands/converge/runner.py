# meiva:note
# The loop that runs a converge step table, and what it does with the failures it collected.
#
# Split out of ``converge_host.py`` (which owns the table and its composition) so the file
# budget of the table module does not decide where this logic lives: the loop is read by
# whoever debugs a host's converge, the table by whoever adds a step.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import assert_never

from cli.commands.converge.incomplete import clear_incomplete
from cli.commands.converge.incomplete import record_incomplete as record_incomplete_marker
from cli.commands.converge.plans.step_selection import RunStep, SkipStep, StepScope, plan_steps
from cli.commands.converge.spec import ConvergeAdvisoryError, ConvergeCtx, ConvergeStep


# meiva:note
# Run what `plan_steps` decides for `steps`, then judge the failures the run collected.
#
# Every selected step runs: a failure is announced and carried to the end of the table instead
# of swallowing the steps after it. On 2026-11-04 one failing step (an external CLI whose
# pinned version could not be installed) aborted every converge on the ubuntu node before the
# OS jobs were registered, and a cold start returns 1 at the converge step, so the host could
# not come up either (task #6233).
def run_converge(ctx: ConvergeCtx, steps: tuple[ConvergeStep, ...], *, prod_install: bool) -> None:
    decisions = plan_steps(
        tuple(
            StepScope(
                step.name, step.roles, step.requires_unit_config, step.host_global, step.services
            )
            for step in steps
        ),
        roles=ctx.roles,
        services=ctx.services,
        prod_install=prod_install,
    )
    print("\n→ converge host")
    blocking, advisory = _apply_steps(ctx, steps, decisions)
    _judge_failures(ctx, blocking, advisory, prod_install=prod_install)


# meiva:note
# Apply the selected steps in order and split what failed: blocking failures decide the exit,
# advisory ones only the host's start. Both are printed where they happen, next to the step's
# own output, so a reader of the run sees which step produced which line.
def _apply_steps(
    ctx: ConvergeCtx,
    steps: tuple[ConvergeStep, ...],
    decisions: tuple[RunStep | SkipStep, ...],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    blocking: list[tuple[str, str]] = []
    advisory: list[tuple[str, str]] = []
    for step, decision in zip(steps, decisions, strict=True):
        match decision:
            case SkipStep(reason=reason):
                print(f"  · {step.name}: skipped ({reason})")
                continue
            case RunStep():
                pass
            case _:
                assert_never(decision)
        try:
            step.apply(ctx)
        except Exception as e:
            (advisory if step.advisory else blocking).append((step.name, str(e)))
            print(f"  {'!' if step.advisory else '✗'} {step.name}: {e}", file=sys.stderr)
            continue
        print(f"  ✓ {step.name}")
    return blocking, advisory


# meiva:note
# What the collected failures mean for the home and for the caller: a blocking failure records
# the marker `meiva status` reports (first one named) and raises; an advisory-only run clears
# the marker and raises `ConvergeAdvisoryError`, which a caller that must keep the host coming
# up treats as a warning. A clean run clears the marker.
# meiva:link cli/commands/converge/docs/converge-incomplete.meiva.okf.md
def _judge_failures(
    ctx: ConvergeCtx,
    blocking: list[tuple[str, str]],
    advisory: list[tuple[str, str]],
    *,
    prod_install: bool,
) -> None:
    if blocking:
        record_incomplete(
            ctx.meiva_home,
            step=blocking[0][0],
            error=blocking[0][1],
            prod_install=prod_install,
            now=ctx.clock.now(),
        )
    elif prod_install:
        clear_incomplete(ctx.meiva_home)
    failed = blocking + advisory
    if failed:
        print(
            f"  ! {len(failed)} step(s) failed; every other step applied: "
            + ", ".join(name for name, _ in failed),
            file=sys.stderr,
        )
    if blocking:
        raise RuntimeError(
            "converge failed: " + "; ".join(f"{name}: {reason}" for name, reason in blocking)
        )
    if advisory:
        raise ConvergeAdvisoryError(advisory)


# meiva:note
# Only the host's prod install owns the marker: a dev cluster's converge must
# never clear or set the prod home's record.
# meiva:link cli/commands/converge/docs/converge-incomplete.meiva.okf.md
def record_incomplete(
    meiva_home: Path, *, step: str, error: str, prod_install: bool, now: datetime
) -> None:
    if not prod_install:
        return
    record_incomplete_marker(meiva_home, step=step, error=error, now=now)
