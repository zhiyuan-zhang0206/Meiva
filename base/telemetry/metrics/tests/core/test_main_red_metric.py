from __future__ import annotations

import pytest

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.telemetry.metrics.core.catalog import collect_core_metrics
from base.telemetry.metrics.grafana_dashboard import render_dashboard
from base.telemetry.metrics.plugin_metrics import MetricSpec


def _targets(spec: MetricSpec) -> list[dict[str, object]]:
    rendered = render_dashboard([spec], [])
    panels = rendered["panels"]
    assert is_object_list(panels)
    panel = next(item for item in panels if is_object_dict(item) and item.get("id") == 2506)
    assert is_object_dict(panel)
    targets = panel["targets"]
    assert is_object_list(targets)
    typed = [target for target in targets if is_object_dict(target)]
    assert len(typed) == len(targets)
    return typed


def test_log_record_limit_keeps_the_table_on_a_bounded_range_query() -> None:
    spec = next(spec for spec in collect_core_metrics() if spec.name == "core_main_red_suspects")
    targets = _targets(spec)
    assert all(target["queryType"] == "range" for target in targets)
    assert all(target["maxLines"] == 1 and target["direction"] == "backward" for target in targets)
    assert all(target["legendFormat"] for target in targets)
    ordinary = spec.model_copy(update={"grafana_max_lines": None})
    targets = _targets(ordinary)
    assert all(target["queryType"] == "instant" for target in targets)
    assert all("maxLines" not in target for target in targets)


@pytest.mark.parametrize(
    "override", [{"grafana_max_lines": 0}, {"query_type": "promql"}, {"output": ["inspector"]}]
)
def test_log_record_limit_rejects_invalid_or_unhandled_surfaces(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        MetricSpec.model_validate(
            {
                **next(
                    spec for spec in collect_core_metrics() if spec.name == "core_main_red_suspects"
                ).model_dump(),
                **override,
            }
        )
