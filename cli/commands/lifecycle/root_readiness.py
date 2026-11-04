"""Fresh protocol readiness for the selected root-owned service roster."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

from base.clock import Timers
from base.deploy import start_trace
from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS
from base.log import logger
from base.native_process.root_control.client import RootClient
from base.native_process.root_control.ipc import UnitState
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.root_control.poll_plan import (
    Expired,
    PollStep,
    ReadinessRound,
    Retry,
    observe_readiness,
    unmet_step,
)
from cli.commands.lifecycle.root_control.snapshot import RootSnapshot, UnitRow, root_units
from cli.commands.probe import ReadinessWait
from services.supervision.roster.service_spec import ServiceSpec


def health_verdicts(
    specs: tuple[ServiceSpec, ...], snapshot: RootSnapshot | None
) -> dict[str, str]:
    """Probe now instead of trusting a cached health result.

    A probe that raises is logged and reads as "unavailable"."""
    if snapshot is None:
        return {}
    verdicts: dict[str, str] = {}
    for spec in specs:
        if spec.identity_probe is None:
            continue
        try:
            verdicts[spec.session] = spec.identity_probe().verdict.value
        except Exception:
            logger.opt(exception=True).warning(
                "identity probe for {} raised; treating it as unavailable", spec.session
            )
            verdicts[spec.session] = "unavailable"
    return verdicts


def _read_snapshot(client: RootClient) -> RootSnapshot | None:
    try:
        return root_driver.root_status(client)
    except root_driver.RootDriverError as exc:
        logger.warning("readiness poll: {}", exc)
        return None


def _running_specs(
    specs: tuple[ServiceSpec, ...], units: Mapping[str, UnitRow]
) -> tuple[ServiceSpec, ...]:
    return tuple(
        spec
        for spec in specs
        if (row := units.get(spec.session)) is not None and row["state"] == UnitState.RUNNING
    )


def _partition_roster(
    roster: tuple[ServiceSpec, ...],
) -> tuple[dict[str, ServiceSpec], tuple[ServiceSpec, ...], tuple[ServiceSpec, ...]]:
    by_session: dict[str, ServiceSpec] = {}
    critical: list[ServiceSpec] = []
    optional: list[ServiceSpec] = []
    for spec in roster:
        by_session[spec.session] = spec
        if spec.session in CRITICAL_SERVICE_SESSIONS:
            critical.append(spec)
        else:
            optional.append(spec)
    return by_session, tuple(critical), tuple(optional)


def _observe_round(
    client: RootClient,
    *,
    sessions: list[str],
    critical_specs: tuple[ServiceSpec, ...],
    optional_specs: tuple[ServiceSpec, ...],
    recorded: frozenset[str],
    started_at: float,
    timers: Timers,
    timeout_s: float,
    poll_interval_s: float,
) -> tuple[ReadinessRound, float, PollStep | None]:
    snapshot = _read_snapshot(client)
    units = root_units(snapshot) if snapshot is not None else {}
    verdicts = health_verdicts(_running_specs(critical_specs, units), snapshot)
    observed = observe_readiness(sessions, units, verdicts, recorded)
    elapsed_s = timers.monotonic() - started_at
    unmet = (
        None
        if observed.settled
        else unmet_step(elapsed_s=elapsed_s, timeout_s=timeout_s, interval_s=poll_interval_s)
    )
    if unmet is None or isinstance(unmet, Expired):
        running_optional = _running_specs(optional_specs, units)
        if running_optional:
            verdicts = {**verdicts, **health_verdicts(running_optional, snapshot)}
            observed = observe_readiness(sessions, units, verdicts, recorded)
    return observed, elapsed_s, unmet


def _build_wait_result(
    by_session: Mapping[str, ServiceSpec], observed: ReadinessRound, elapsed_s: float
) -> ReadinessWait:
    return ReadinessWait(
        tuple(by_session[session] for session in observed.unready),
        elapsed_s,
        sessions_gone=observed.sessions_gone,
        non_critical_unready=tuple(
            by_session[session] for session in observed.non_critical_unready
        ),
    )


# meiva:note
# Wait for the core services of the same root-owned roster after launch.
#
# Intermediate rounds probe only running critical services so unready optional services never add
# synchronous dial latency to core bring-up; the settling or expiring round probes running
# non-critical services once against the same root snapshot for accurate diagnostic reporting.
def wait_for_service_tree(
    roster: tuple[ServiceSpec, ...],
    *,
    timeout_s: float,
    home: Path,
    timers: Timers,
    environ: Mapping[str, str],
    pid: int,
    poll_interval_s: float = root_driver.READY_POLL_INTERVAL_S,
) -> ReadinessWait:
    client = root_driver.root_client(home, environ=environ, pid=pid)
    by_session, critical_specs, optional_specs = _partition_roster(roster)
    sessions = list(by_session)
    started_at = timers.monotonic()
    recorded: set[str] = set()
    while True:
        observed, elapsed_s, unmet = _observe_round(
            client,
            sessions=sessions,
            critical_specs=critical_specs,
            optional_specs=optional_specs,
            recorded=frozenset(recorded),
            started_at=started_at,
            timers=timers,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        for session in observed.newly_ready:
            recorded.add(session)
            start_trace.record_service_ready(session, elapsed_s)
        result = _build_wait_result(by_session, observed, elapsed_s)
        if unmet is None:
            return result
        match unmet:
            case Retry(after_s):
                timers.sleep(after_s)
            case Expired():
                return result
            case _:
                assert_never(unmet)
