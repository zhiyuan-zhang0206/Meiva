from __future__ import annotations

import json
from dataclasses import asdict

from cli.database import OperatorDatabaseFactory
from services.upkeep.events_maintenance import legacy_usage_import as legacy


def _totals(totals: legacy.Totals) -> dict[str, object]:
    return {**asdict(totals), "cost_usd": str(totals.cost_usd)}


def _report(conn_scan: legacy.Scan) -> dict[str, object]:
    return {
        "cutoff_day": conn_scan.cutoff.isoformat(),
        "first_day": conn_scan.first_day.isoformat() if conn_scan.first_day else None,
        "last_day": conn_scan.last_day.isoformat() if conn_scan.last_day else None,
        "rows": conn_scan.rows,
        "skipped_zero_call_rows": conn_scan.skipped_zero_call_rows,
        "unpriced_rows": conn_scan.unpriced_rows,
        "unpriced_calls": conn_scan.unpriced_calls,
        "partly_priced_rows": conn_scan.partly_priced_rows,
        "partly_priced_unpriced_calls": conn_scan.partly_priced_unpriced_calls,
        "estimated_calls": conn_scan.estimated_calls,
        "by_billing": {k: _totals(v) for k, v in sorted(conn_scan.by_billing.items())},
        "by_model": [
            {"billing": billing, "model": model, **_totals(totals)}
            for (billing, model), totals in sorted(conn_scan.by_model.items())
        ],
        "mixed_billing_rows": [
            {
                "day": r.day.isoformat(),
                "agent_id": r.agent_id,
                "model": r.model,
                "llm_calls": r.llm_calls,
                "cost_usd": r.cost_usd,
                "cost_usd_subscription": r.cost_usd_subscription,
                "unpriced_calls": r.unpriced_calls,
                "unpriced_calls_subscription": r.unpriced_calls_subscription,
            }
            for r in conn_scan.mixed
        ],
    }


def cmd_import_legacy_usage(
    database_factory: OperatorDatabaseFactory,
    *,
    cluster: str,
    apply: bool,
    batch_size: int,
) -> int:
    with database_factory().connect() as conn:
        scanned = legacy.scan(conn, batch_size=batch_size)
        report = _report(scanned) | {
            "mode": "applied" if apply else "dry-run",
            "overlap_days": legacy.overlap_days(conn, scanned.cutoff),
            "total_check": legacy.total_check(conn),
        }
        if apply:
            try:
                counts = legacy.import_days(
                    conn, cutoff=scanned.cutoff, cluster=cluster, batch_size=batch_size
                )
            except legacy.MixedBillingError as blocked:
                report["blocked"] = str(blocked)
                print(json.dumps(report, sort_keys=True))
                return 2
            report["import"] = asdict(counts)
            report["ledger_after"] = legacy.imported_totals(conn)
    print(json.dumps(report, sort_keys=True))
    return 0
