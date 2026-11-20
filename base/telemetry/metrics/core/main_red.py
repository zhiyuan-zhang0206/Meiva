# meiva:note
# Each slot reads the latest round before blank rows are removed. Filtering in
# LogQL would resurrect an older nonempty suspect after its current slot clears.
from __future__ import annotations

from base.telemetry.metrics.logql import EVENT_NAME_SELECTOR
from base.telemetry.metrics.plugin_metrics import MetricSpec


def _slot(index: int) -> str:
    return (
        EVENT_NAME_SELECTOR
        + ' | json | cluster=".meiva" | json row="attributes.suspects['
        + str(index)
        + ']" | line_format `{{.row | default "{\\"fingerprint\\":\\"\\"}"}}`'
    )


def core_metrics() -> list[MetricSpec]:
    return [
        MetricSpec(
            name="core_main_red_suspects",
            title="Main-red suspects - latest round",
            description="Oldest 10 suspects from the latest main_red_watch_round snapshot. Age is measured at that round; Time shows its freshness. Each query takes the latest round before filtering blank slots, so a resolved suspect does not reappear from an older round. The event carries suspects_dropped when more than 10 exist. No-opportunity and unreadable evidence are explicit.",
            event_name="main_red_watch_round",
            category="telemetry",
            panel="table",
            query_type="logql",
            query=_slot(0),
            targets=[_slot(i) for i in range(1, 10)],
            target_names=[f"suspect {i + 1}" for i in range(10)],
            grafana_max_lines=1,
            panel_id=2506,
            section="Dev/CI",
            order=5,
            width=24,
            height=9,
            thresholds=[],
            options={
                "showHeader": True,
                "cellHeight": "sm",
                "sortBy": [{"displayName": "age_seconds", "desc": True}],
            },
            transformations=[
                {
                    "id": "extractFields",
                    "options": {
                        "source": "Line",
                        "format": "json",
                        "replace": True,
                        "keepTime": True,
                    },
                },
                {"id": "merge", "options": {}},
                {
                    "id": "filterByValue",
                    "options": {
                        "type": "include",
                        "match": "all",
                        "filters": [
                            {
                                "fieldName": "fingerprint",
                                "config": {"id": "regex", "options": {"value": ".+"}},
                            }
                        ],
                    },
                },
            ],
        )
    ]
