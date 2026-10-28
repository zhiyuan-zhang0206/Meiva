from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

from base.clock import Clock
from base.clock.ids import new_uuid
from base.host.permissions_helper.client import HelperFactory
from base.host.permissions_helper.grants import gate, probe, triggers
from base.host.permissions_helper.grants.catalog import PermissionItem
from base.host.permissions_helper.grants.gate import GateReport, GateState
from base.host.permissions_helper.grants.probe import HelperChannel, PermissionsProbeError

REQUEST_TIMEOUT_S = 90.0


# meiva:note
# The host the gate runs on, as the process root that runs it describes it. `client` is the helper's
# channel, None on a platform that has no helper (the gate has nothing to check there); `env` is the
# environment the probe children are launched with, `workdir` where they write, `home` the account
# home whose folders the folder rows ask, and `record_path` the file that remembers the last pass.
@dataclass(frozen=True)
class GateHost:
    client: HelperChannel | None
    env: dict[str, str]
    workdir: Path
    home: Path
    record_path: Path


# meiva:note
# Describe a host from the facts a root already holds: the Meiva `home` (the scratch directory and
# the record live under it), the account's `user_home`, the launch `environ`, and the helper factory
# the platform decided (None where it has no helper).
def gate_host(
    *,
    home: Path,
    user_home: Path,
    environ: Mapping[str, str],
    helper: HelperFactory | None,
) -> GateHost:
    from base.sessions import env_forwarding

    return GateHost(
        client=None if helper is None else helper(),
        env=env_forwarding.forward_env_dict(environ, activate_venv=False),
        workdir=home / "permissions-onboarding",
        home=user_home,
        record_path=gate.record_path(home),
    )


# meiva:note
# After requesting, the gate re-probes and reports what the system says. A child that reports 'granted' while the system still denies leaves the item denied, and a request that raised no dialog (macOS answers a denied or already-asked grant silently) leaves the grant exactly as it was.
# meiva:link services/desktop/permissions_helper/docs/onboarding.meiva.okf.md
def check(*, host: GateHost, now: datetime, run_id: str) -> GateReport:
    if host.client is None:
        return gate.not_applicable(now)
    record = gate.read_record(host.record_path)
    deferred = bool(record.deferred) if record is not None else False
    result = probe.probe(host.client, workdir=host.workdir, run_id=run_id, env=host.env)
    report = gate.evaluate(result, deferred=deferred, now=now)
    gate.remember(report, deferred=deferred, path=host.record_path)
    return report


# meiva:note
# One progress line for the person running the pass; nothing when no stream was given.
def _say(progress: TextIO | None, line: str) -> None:
    if progress is not None:
        progress.write(line + "\n")
        progress.flush()


def _ask_process_grants(
    report: GateReport,
    client: HelperChannel,
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
        probe.request_process_grants(client)
    except PermissionsProbeError as exc:
        return f"Asking the system for a grant failed: {exc}"
    return ""


def _ask_items(
    seat: GateHost,
    client: HelperChannel,
    pending: tuple[PermissionItem, ...],
    *,
    run_id: str,
    timeout_s: float,
    progress: TextIO | None,
) -> None:
    for item in pending:
        if item.group == "desktop":
            continue
        _say(progress, f"Asking for: {item.title}")
        verdict = triggers.request_item(
            client,
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
    host: GateHost,
    clock: Clock,
    run_id: str,
    automatic: bool = True,
    timeout_s: float = REQUEST_TIMEOUT_S,
    progress: TextIO | None = None,
) -> GateReport:
    report = check(host=host, now=clock.now(), run_id=f"{run_id}-probe")
    client = host.client
    if client is None or report.state in (GateState.COMPLETE, GateState.NOT_APPLICABLE):
        return report
    pending = report.prompt_now() if automatic else report.requestable()
    if not pending:
        return report
    unsupported = _ask_process_grants(report, client, pending, progress)
    _ask_items(
        host, client, pending, run_id=f"{run_id}-ask", timeout_s=timeout_s, progress=progress
    )
    report = check(host=host, now=clock.now(), run_id=f"{run_id}-verify")
    if unsupported and not report.complete:
        report = replace(
            report, diagnostic=" ".join(part for part in (report.diagnostic, unsupported) if part)
        )
    return report


def observations(report: GateReport) -> dict[str, str]:
    return {status.item.id: status.grant.value for status in report.items}


def defer(*, host: GateHost, now: datetime) -> None:
    gate.mark_deferred(now=now, path=host.record_path)


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


# meiva:note
# The gate as the gateway serves it: the host it was built for, the clock every pass is stamped
# with, and the generator its run ids are drawn from. Each call is one pass.
class GateService:
    def __init__(self, *, host: GateHost, clock: Clock, rng: random.Random) -> None:
        self.host = host
        self.clock = clock
        self.rng = rng

    def check(self) -> GateReport:
        return check(host=self.host, now=self.clock.now(), run_id=self._run_id())

    def onboard(
        self,
        *,
        automatic: bool = True,
        timeout_s: float = REQUEST_TIMEOUT_S,
        progress: TextIO | None = None,
    ) -> GateReport:
        return onboard(
            host=self.host,
            clock=self.clock,
            run_id=self._run_id(),
            automatic=automatic,
            timeout_s=timeout_s,
            progress=progress,
        )

    def defer(self) -> None:
        defer(host=self.host, now=self.clock.now())

    def restart_helper(self) -> str:
        return restart_helper()

    def _run_id(self) -> str:
        return new_uuid(self.rng).hex
