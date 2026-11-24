from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, time

from cli.database import OperatorDatabaseFactory
from services.upkeep.events_maintenance.unpriced_backfill import (
    UnpricedBackfillReport,
    load_price_book,
    run_unpriced_backfill,
)
from services.upkeep.events_maintenance.usage_ledger_backfill import (
    backfill_usage_ledger,
    reconcile_usage_ledger,
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


def cmd_backfill_usage_ledger(
    database_factory: OperatorDatabaseFactory,
    *,
    start: date,
    end: date,
    batch_size: int,
) -> int:
    with database_factory().connect() as conn:
        counts = backfill_usage_ledger(
            conn,
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=datetime.combine(end, time.min, tzinfo=UTC),
            batch_size=batch_size,
            prices=load_price_book(),
        )
    print(json.dumps(asdict(counts), sort_keys=True))
    return 0


def cmd_reconcile_usage_ledger(
    database_factory: OperatorDatabaseFactory,
    *,
    start: date,
    end: date,
    batch_size: int,
) -> int:
    with database_factory().connect() as conn:
        report = reconcile_usage_ledger(
            conn,
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=datetime.combine(end, time.min, tzinfo=UTC),
            batch_size=batch_size,
            prices=load_price_book(),
        )
    print(json.dumps(asdict(report) | {"matches": report.matches}, default=str, sort_keys=True))
    return int(not report.matches)
