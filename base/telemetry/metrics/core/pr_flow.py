# meiva:note
# Core PR-flow panels (task #2139) — a separate registration module.
#
# The table keeps the latest reported median and p90 for each complete cluster-local day.
# The daily job re-emits its trailing window. After 26 hours a missed run leaves a gap.
# meiva:link deploy/lgtm/config/grafana/provisioning/dashboards/README.md


from __future__ import annotations

from base.telemetry.metrics.plugin_metrics import MetricSpec


def _daily_latency_query(percentile: str) -> str:
    return (
        'last_over_time({service_name="unknown_service", event_name={event_name}} '
        '| json day="attributes.day", cluster="cluster", category="category", '
        f'seconds="attributes.ready_to_merge_{percentile}_seconds" '
        f'| cluster=".meiva" | category={{category}} | label_format percentile="{percentile}" '
        '| unwrap seconds | __error__="" [26h]) by (day, percentile)'
    )


def core_metrics() -> list[MetricSpec]:
    """This module's core metrics, in registration order."""
    return [
        MetricSpec(
            name="core_pr_flow_ready_to_merge",
            title="PR ready to merged \u2014 median / p90 (by day)",
            description=(
                "Ready-for-review to merge latency by complete cluster-tz day "
                "\u2014 median and p90 seconds from the daily PR-flow export "
                "job (scripts/ci/pull_requests/pr_flow_export.py). One row per day, "
                "using the latest report in the 26h lookback."
            ),
            event_name="pr_flow_daily",
            category="telemetry",
            unit="s",
            panel="table",
            query=f"{_daily_latency_query('median')} or {_daily_latency_query('p90')}",
            query_type="logql",
            target_names=["{{percentile}}"],
            thresholds=[],
            panel_id=2401,
            section="PR flow",
            order=0,
            transformations=[
                {"id": "labelsToFields", "options": {}},
                {
                    "id": "groupingToMatrix",
                    "options": {
                        "columnField": "percentile",
                        "rowField": "day",
                        "valueField": "Value #A",
                    },
                },
                {"id": "organize", "options": {"renameByName": {"day\\percentile": "day"}}},
            ],
        )
    ]
