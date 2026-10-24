# meiva:note
# What the daily debt sweep tells the clearing worker, the telemetry and the P0 lead.
#
# The worker always gets a prompt, whether the scan succeeded or failed: it re-checks the findings
# against the report artifact and reconciles the single ledger. The event payload mirrors the
# declared `debt_sweep_daily` event. The dry run renders exactly what a real run would send,
# with a preview dispatch, so an operator can read it without touching the cluster.
# meiva:link schedules/docs/debt-sweep.meiva.okf.md

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from schedules.plans.debt_sweep_scan import ScanReport

__all__ = [
    "DEFAULT_WORKER_LABEL",
    "LEDGER_PATH",
    "dry_run_text",
    "event_payload",
    "failure_message",
    "log_line",
    "parse_worker_label",
    "worker_prompt",
]

LEDGER_PATH = "future/tech-debt/ledger.md"
DEFAULT_WORKER_LABEL = "debt-sweep-daily"
_FAILURE_TAIL = 1000


def worker_prompt(day: str, artifact: Path, scan: ScanReport) -> str:
    return f"""You are today's debt-clearing pass for the Meiva repository ({day}).

Start a fresh worktree from origin/main and follow the normal PR workflow; never push main.
Use the `meiva.skills.sweeper` engine with `docs/conventions/engineering/tech-debt.md` as project input.
Reconcile the single ledger at `{LEDGER_PATH}`.

The mechanical scan artifact is `{artifact}`.
Scan summary: {scan.summary}
Use the artifact as evidence, re-check findings as needed, and keep the full mechanical lists in
the PR body rather than the ledger. Record only strong, fingerprinted findings; respect wontfix
entries and remove resolved entries.

Deliver at most ONE `chore(sweeper): debt reconcile {day}` PR. If reconciliation changes nothing,
open no PR and report the no-op. When done, report your result and end your own process.
"""


def event_payload(
    day: str, scan: ScanReport, action: str, worker_agent_id: int
) -> dict[str, str | int]:
    return {
        "day": day,
        "scan_status": "ok" if scan.succeeded else "failed",
        "action": action,
        "worker_agent_id": worker_agent_id,
    }


def failure_message(detail: str) -> str:
    return (
        "Daily debt sweep failed:\n"
        f"{detail[-_FAILURE_TAIL:]}\n"
        "Check the schedule log and the saved mechanical-scan report, then run the "
        "debt-clearing pass manually with the sweeper skills."
    )


# meiva:note
# `MEIVA_DEBT_SWEEP_AGENT_LABEL`: unset or blank means the default label, anything else (trimmed)
# is the label of the agent that clears the debt.
def parse_worker_label(raw: str | None) -> str:
    if raw is not None and raw.strip():
        return raw.strip()
    return DEFAULT_WORKER_LABEL


def log_line(now: datetime, day: str, scan: ScanReport, action: str, worker_agent_id: int) -> str:
    status = "ok" if scan.succeeded else "failed"
    return (
        f"[{now.isoformat()}] debt-sweep-daily: {day} \u2014 "
        f"scan={status}, {action} worker {worker_agent_id}"
    )


# meiva:note
# Everything the dry run prints, in order. The preview dispatch is a spawn of agent 0, which no
# real run produces.
def dry_run_text(day: str, artifact: Path, scan: ScanReport, label: str) -> str:
    report = scan.text if scan.text.endswith("\n") else scan.text + "\n"
    payload = json.dumps(event_payload(day, scan, "spawned", 0), sort_keys=True)
    return (
        f"dry-run: mechanical scan report would be written to: {artifact}\n"
        f"dry-run: mechanical scan summary: {scan.summary}\n"
        "dry-run: mechanical scan report:\n"
        f"{report}"
        f"dry-run: resolved worker label: {label}\n"
        "dry-run: prompt preview:\n"
        f"{worker_prompt(day, artifact, scan)}"
        "dry-run: event payload:\n"
        f"{payload}\n"
        "dry-run: no claims, agent operations, telemetry, or database access were performed.\n"
    )
