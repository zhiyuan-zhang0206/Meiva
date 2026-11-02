# meiva:note
# Core PR-flow panels (task #2139) — a separate registration module.
#
# The PR-flow tile reads what the daily macmini export job
# (``scripts/ci/pull_requests/pr_flow_export.py``, ``pr_flow_daily`` event)
# publishes as OTLP gauges through ``base/telemetry/otlp/telemetry_otlp.py``: one absolute
# sample per complete cluster-tz day in a rolling 30-day window, re-emitted on
# every run.
#
# Every sample is a day-labeled gauge series (``day`` rides along as a datapoint
# attribute, alongside the exporter process dimensions). Two query shapes follow
# from that:
#
# - ``max by (day)`` — collapse the per-run process/machine multiplicity (and a
#   manual re-run) onto one series per day; a plain read would draw several
#   overlapping lines per day.
# - ``last_over_time(...[26h])`` — the exporter is a one-shot process, so a day
#   series only receives a sample while the daily run keeps re-emitting it; 26h
#   keeps the last sample alive across the daily cadence (a missed run drops the
#   day instead of pinning a stale point).
#
# The panel SQL/JSON mirror lives in
# ``deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json``
# (row "PR flow") and is locked by ``tests/components/plugins/test_plugin_metrics_logql.py``.
# meiva:link deploy/lgtm/config/grafana/provisioning/dashboards/README.md


from __future__ import annotations

from base.telemetry.metrics.plugin_metrics import MetricSpec


def core_metrics() -> list[MetricSpec]:
    """This module's core metrics, in registration order."""
    specs: list[MetricSpec] = []
    specs.append(
        MetricSpec(
            name="core_pr_flow_ready_to_merge",
            title="PR ready to merged \u2014 median / p90 (by day)",
            description=(
                "Ready-for-review to merge latency by complete cluster-tz day "
                "\u2014 median and p90 seconds, sampled by the daily PR-flow export "
                "job (scripts/ci/pull_requests/pr_flow_export.py) into the "
                "meiva_pr_flow_daily_ready_to_merge_median_seconds / _p90_seconds "
                "gauges. One row per day; a day leaves the table once its last "
                "sample ages past the 26h lookback (a missed run shows as a gap)."
            ),
            event_name="pr_flow_daily",
            category="telemetry",
            unit="s",
            panel="table",
            query="max by (day) (last_over_time(meiva_pr_flow_daily_ready_to_merge_median_seconds[26h]))",
            query_type="promql",
            targets=[
                "max by (day) (last_over_time(meiva_pr_flow_daily_ready_to_merge_p90_seconds[26h]))"
            ],
            target_names=["median", "p90"],
            thresholds=[],
            panel_id=2401,
            section="PR flow",
            order=0,
            transformations=[
                {"id": "joinByField", "options": {"byField": "day", "mode": "outer"}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time 1": True, "Time 2": True},
                        "renameByName": {"Value #A": "median", "Value #B": "p90"},
                    },
                },
            ],
        )
    )
    return specs
