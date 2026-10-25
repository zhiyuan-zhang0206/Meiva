from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

from base.host.permissions_helper.grants import gate, probe, triggers
from base.host.permissions_helper.grants.catalog import PermissionItem
from base.host.permissions_helper.grants.gate import GateReport, GateState
from base.host.permissions_helper.grants.probe import HelperChannel, PermissionsProbeError
from base.paths import meiva_home

REQUEST_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class GateHost:
    client: HelperChannel
    env: dict[str, str]
    workdir: Path
    home: Path
    record_path: Path
    platform: str


def scratch_dir() -> Path:
    return meiva_home() / "permissions-onboarding"


def child_env() -> dict[str, str]:
    from base.host.env.registry import process_environment
    from base.sessions import env_forwarding

    return env_forwarding.forward_env_dict(process_environment(), activate_venv=False)


def helper_client() -> HelperChannel:
    from base.sessions.helperproc import helper_client as sessions_helper_client

    return sessions_helper_client()


def local_host() -> GateHost:
    return GateHost(
        client=helper_client(),
        env=child_env(),
        workdir=scratch_dir(),
        home=Path.home(),
        record_path=gate.record_path(),
        platform=sys.platform,
    )


# meiva:note
# After requesting, the gate re-probes and reports what the system says. A child that reports 'granted' while the system still denies leaves the item denied, and a request that raised no dialog (macOS answers a denied or already-asked grant silently) leaves the grant exactly as it was.
# meiva:link services/desktop/permissions_helper/docs/onboarding.meiva.okf.md
def check(
    *,
    host: GateHost | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> GateReport:
    seat = host or local_host()
    if seat.platform != "darwin":
        return gate.not_applicable(now)
    record = gate.read_record(seat.record_path)
    deferred = bool(record.deferred) if record is not None else False
    result = probe.probe(
        seat.client,
        workdir=seat.workdir,
        run_id=run_id or f"{time.monotonic_ns():x}",
        env=seat.env,
    )
    report = gate.evaluate(result, deferred=deferred, now=now)
    gate.remember(report, deferred=deferred, path=seat.record_path)
    return report


# meiva:note
# One progress line for the person running the pass; nothing when no stream was given.
def _say(progress: TextIO | None, line: str) -> None:
    if progress is not None:
        progress.write(line + "\n")
        progress.flush()


def _ask_process_grants(
    report: GateReport,
    seat: GateHost,
    pending: tuple[PermissionItem, ...],
    progress: TextIO | None,
) -> str:
    items = [item for item in pending if item.group == "desktop"]
    if not items:
        return ""
    if not report.supports_request:
        return (
            "The running permissions helper cannot request grants: its build predates"
            " request_grants_v1. Update the helper, then run the gate again."
        )
    _say(progress, "Asking the system for: " + ", ".join(item.id for item in items))
    try:
        probe.request_process_grants(seat.client)
    except PermissionsProbeError as exc:
        return f"Asking the system for a grant failed: {exc}"
    return ""


def _ask_items(
    seat: GateHost,
    pending: tuple[PermissionItem, ...],
    *,
    timeout_s: float,
    progress: TextIO | None,
) -> None:
    run_id = f"{time.monotonic_ns():x}"
    for item in pending:
        if item.group == "desktop":
            continue
        _say(progress, f"Asking for: {item.title}")
        verdict = triggers.request_item(
            seat.client,
            item,
            workdir=seat.workdir,
            run_id=run_id,
            env=seat.env,
            home=seat.home,
            timeout_s=timeout_s,
        )
        _say(progress, f"  -> {verdict}")


# meiva:note
# onboard probes first and then asks. An automatic pass (the converge step, so every start) requests exactly the rows the system reports not_determined -- Screen Recording and Accessibility through the helper's `request_grants`, folders and Automation targets through their own children. A row whose record is denied is not asked again automatically, and a stale row (record granted, helper not restarted) is never asked at all: neither can be resolved by another dialog. An explicit pass (the `meiva permissions onboard` verb, the console's Ask button) also asks denied prompt-mode rows, which is the only way back to a denied grant without a terminal. No caller attests that a person is present: a dialog nobody answers stays pending, and that cost was chosen over discovering a missing grant in the middle of a task.
# meiva:link services/desktop/permissions_helper/docs/onboarding.meiva.okf.md
def onboard(
    *,
    host: GateHost | None = None,
    now: datetime | None = None,
    automatic: bool = True,
    timeout_s: float = REQUEST_TIMEOUT_S,
    progress: TextIO | None = None,
) -> GateReport:
    seat = host or local_host()
    report = check(host=seat, now=now)
    if report.state in (GateState.COMPLETE, GateState.NOT_APPLICABLE):
        return report
    pending = report.prompt_now() if automatic else report.requestable()
    if not pending:
        return report
    unsupported = _ask_process_grants(report, seat, pending, progress)
    _ask_items(seat, pending, timeout_s=timeout_s, progress=progress)
    report = check(host=seat, now=now)
    if unsupported and not report.complete:
        report = replace(
            report, diagnostic=" ".join(part for part in (report.diagnostic, unsupported) if part)
        )
    return report


def observations(report: GateReport) -> dict[str, str]:
    return {status.item.id: status.grant.value for status in report.items}


def defer(*, now: datetime | None = None, host: GateHost | None = None) -> None:
    seat = host or local_host()
    gate.mark_deferred(now=now, path=seat.record_path)


# meiva:link services/desktop/permissions_helper/docs/onboarding.meiva.okf.md
def restart_helper() -> str:
    return (
        "The permissions helper re-reads a grant only at process start, and it is the"
        " ancestor of this home's root -- replacing it from inside the tree takes the"
        " agent plane down with it. Retire and recreate it under custody instead:"
        " `meiva stop`, then `meiva start` (each with its explicit choices; see their `--help`). Never `launchctl kickstart -k`."
    )


def missing_summary(report: GateReport) -> str:
    if report.state is GateState.UNAVAILABLE:
        return report.diagnostic
    if not report.missing:
        return ""
    names = ", ".join(status.item.title for status in report.missing)
    return f"{len(report.missing)} permission(s) still missing: {names}"


class GateService:
    def check(self) -> GateReport:
        return check()

    def onboard(
        self,
        *,
        automatic: bool = True,
        timeout_s: float = REQUEST_TIMEOUT_S,
        progress: TextIO | None = None,
    ) -> GateReport:
        return onboard(automatic=automatic, timeout_s=timeout_s, progress=progress)

    def defer(self) -> None:
        defer()

    def restart_helper(self) -> str:
        return restart_helper()
