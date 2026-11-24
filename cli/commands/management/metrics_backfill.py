from __future__ import annotations

import json
from datetime import date

from cli.database import OperatorDatabaseFactory
from services.upkeep.events_maintenance.unpriced_backfill import (
    UnpricedBackfillReport,
    run_unpriced_backfill,
)


def _render_report(report: UnpricedBackfillReport) -> str:
    mode = "dry-run" if report.dry_run else "applied"
    lines = [
        f"mode:                {mode}",
        f"observations:        {report.observations_priced}/{report.observations_scanned} priced "
        f"({report.observations_still_unpriced} still unpriced)",
        f"metric days:         {report.metric_days_recomputed} recomputed",
        f"added cost (usd):    {report.added_observation_cost_usd:.4f} "
        f"(subscription {report.added_observation_cost_usd_subscription:.4f})",
        f"ledger days:         {report.ledger_days_updated}/{report.ledger_days_scanned} updated "
        f"({report.ledger_calls_priced} calls priced)",
        f"totals rebuilt:      {'yes' if report.totals_rebuilt else 'no'}",
    ]
    if report.by_model:
        lines.append("")
        lines.append("by model:")
        for item in report.by_model:
            arrow = (
                f"{item.raw_model} -> {item.canonical_model}"
                if item.raw_model != item.canonical_model
                else (item.canonical_model or "<empty>")
            )
            lines.append(
                f"  {arrow}: priced={item.observations_priced}, "
                f"still_unpriced={item.observations_still_unpriced}, "
                f"added_cost_usd={item.added_cost_usd:.4f}"
            )
    return "\n".join(lines)


def cmd_metrics_backfill_unpriced(
    database_factory: OperatorDatabaseFactory,
    *,
    today: date,
    apply: bool,
    as_json: bool,
) -> int:
    with database_factory().connect() as conn:
        report = run_unpriced_backfill(conn, today=today, dry_run=not apply)
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_render_report(report))
    return 0
