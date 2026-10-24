"""Fresh protocol readiness for the selected root-owned service roster."""

from __future__ import annotations

import time
from pathlib import Path
from typing import assert_never

from base.deploy import start_trace
from base.log import logger
from base.native_process.root_control.client import RootClient
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.root_control.poll_plan import (
    Expired,
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


# meiva:note
# Observe the service table and ask the current protocol probes.
#
# A root that answers with an unreadable status reads as no root yet: the wait keeps polling
# and ends unready at its deadline, as it did before the snapshot was validated.
def _fresh_round(
    client: RootClient, specs: tuple[ServiceSpec, ...]
) -> tuple[dict[str, UnitRow], dict[str, str]]:
    try:
        snapshot = root_driver.root_status(client)
    except root_driver.RootDriverError as exc:
        logger.warning("readiness poll: {}", exc)
        snapshot = None
    units = root_units(snapshot) if snapshot is not None else {}
    return units, health_verdicts(specs, snapshot)


# meiva:note
# Wait for the core services of the same root-owned roster after launch.
#
# Optional availability is a diagnostic snapshot, never a reason to keep waiting.
def wait_for_service_tree(
    roster: tuple[ServiceSpec, ...],
    *,
    timeout_s: float,
    home: Path,
    poll_interval_s: float = root_driver.READY_POLL_INTERVAL_S,
) -> ReadinessWait:
    client = root_driver.root_client(home)
    by_session = {spec.session: spec for spec in roster}
    started_at = time.monotonic()
    recorded: set[str] = set()
    while True:
        units, verdicts = _fresh_round(client, roster)
        observed = observe_readiness(list(by_session), units, verdicts, frozenset(recorded))
        elapsed_s = time.monotonic() - started_at
        for session in observed.newly_ready:
            recorded.add(session)
            start_trace.record_service_ready(session, elapsed_s)
        result = ReadinessWait(
            tuple(by_session[session] for session in observed.unready),
            elapsed_s,
            sessions_gone=observed.sessions_gone,
            non_critical_unready=tuple(
                by_session[session] for session in observed.non_critical_unready
            ),
        )
        if observed.settled:
            return result
        unmet = unmet_step(elapsed_s=elapsed_s, timeout_s=timeout_s, interval_s=poll_interval_s)
        match unmet:
            case Retry(after_s):
                time.sleep(after_s)
            case Expired():
                return result
            case _:
                assert_never(unmet)
