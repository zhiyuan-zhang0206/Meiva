# meiva:note
# Pure round decision for external harness CLI update schedules.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from base.host.env.harness.catalog import ConvergenceReport


@dataclass(frozen=True)
class RoundDecision:
    telemetry_payload: Mapping[str, object]
    log_line: str
    failure_message: str | None


def decide_round(
    report: ConvergenceReport | None, *, skipped_non_production: bool = False
) -> RoundDecision:
    if skipped_non_production or report is None:
        return RoundDecision(
            telemetry_payload={"status": "skipped", "reason": "non_production"},
            log_line="skipped: non-production identity",
            failure_message=None,
        )

    outcomes_payload = [
        {
            "cli": o.cli,
            "status": o.status,
            "previous": o.previous_version,
            "target": o.target_version,
            "installed": o.installed_version,
            "detail": o.detail,
        }
        for o in report.outcomes
    ]
    telemetry_payload: dict[str, object] = {
        "status": "ok" if report.ok else "failed",
        "updated_count": len(report.updated),
        "failed_count": len(report.failed),
        "outcomes": outcomes_payload,
    }

    n_updated = len(report.updated)
    n_failed = len(report.failed)
    n_uptodate = len([o for o in report.outcomes if o.status == "up_to_date"])
    n_absent = len([o for o in report.outcomes if o.status == "skipped_absent"])

    summary = f"{n_updated} updated, {n_failed} failed, {n_uptodate} up-to-date, {n_absent} absent"
    log_line = f"convergence complete: {summary}"

    failure_message = (
        "external harness CLI update failed: "
        + "; ".join(f"{f.cli} ({f.detail})" for f in report.failed)
        if report.failed
        else None
    )

    return RoundDecision(
        telemetry_payload=telemetry_payload,
        log_line=log_line,
        failure_message=failure_message,
    )
