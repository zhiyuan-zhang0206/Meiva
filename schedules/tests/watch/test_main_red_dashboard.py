from __future__ import annotations

import json
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of


def test_suspect_table_limits_the_latest_round_before_removing_empty_slots() -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    )
    dashboard = object_of(json.loads, path.read_text())
    assert is_object_dict(dashboard)
    panels = dashboard["panels"]
    assert is_object_list(panels)
    panel = next(item for item in panels if is_object_dict(item) and item.get("id") == 2506)
    assert is_object_dict(panel)
    assert panel["type"] == "table"
    targets = panel["targets"]
    assert is_object_list(targets) and len(targets) == 10
    for index, target in enumerate(targets):
        assert is_object_dict(target)
        assert target["maxLines"] == 1 and target["direction"] == "backward"
        expression = target["expr"]
        assert isinstance(expression, str)
        assert 'event_name="main_red_watch_round"' in expression
        assert f'row="attributes.suspects[{index}]"' in expression
        assert 'default "{\\"fingerprint\\":\\"\\"}"' in expression
        assert 'row!=""' not in expression
    transformations = panel["transformations"]
    assert is_object_list(transformations)
    assert is_object_dict(transformations[-1])
    assert transformations[-1]["id"] == "filterByValue"
