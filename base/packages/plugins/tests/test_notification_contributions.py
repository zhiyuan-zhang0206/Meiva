from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from base.packages.plugins.surface.notification_contributions import (
    validate_notification_contributions,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "meiva_builtins" / "plugins"


def _validate(value: object) -> tuple[list[dict[str, object]], list[str]]:
    errors: list[str] = []
    return validate_notification_contributions(value, errors), errors


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"id": "meiva_fleet.task_closed", "label": "Task closed"}
    entry.update(overrides)
    return entry


def test_a_minimal_declaration_is_completed_with_defaults() -> None:
    parsed, errors = _validate([_entry()])
    assert errors == []
    assert parsed == [
        {
            "id": "meiva_fleet.task_closed",
            "label": "Task closed",
            "description": "",
            "importance": "normal",
            "defaultEnabled": False,
            "dedupeWindowSeconds": 0,
        }
    ]


def test_a_category_that_ships_enabled_is_rejected() -> None:
    parsed, errors = _validate([_entry(defaultEnabled=True)])

    assert parsed == []
    assert "must be false" in errors[0]


def test_the_id_must_be_namespaced() -> None:
    parsed, errors = _validate([_entry(id="task_closed")])
    assert parsed == []
    assert "namespaced" in errors[0]


def test_a_duplicate_id_is_rejected() -> None:
    parsed, errors = _validate([_entry(), _entry()])
    assert len(parsed) == 1
    assert "duplicate id" in errors[0]


def test_an_unknown_field_is_rejected() -> None:
    parsed, errors = _validate([_entry(channel="sms")])
    assert parsed == []
    assert "unknown field" in errors[0]


def test_an_unknown_importance_is_rejected() -> None:
    parsed, errors = _validate([_entry(importance="critical")])
    assert parsed == []
    assert "importance" in errors[0]


def test_a_negative_window_is_rejected() -> None:
    parsed, errors = _validate([_entry(dedupeWindowSeconds=-1)])
    assert parsed == []
    assert "dedupeWindowSeconds" in errors[0]


def test_a_non_list_declaration_is_rejected() -> None:
    parsed, errors = _validate({"id": "meiva_fleet.task_closed"})
    assert parsed == []
    assert "must be a list" in errors[0]


# meiva:note
# A shipped plugin category is a request that needs the user's reply, and the user
# decides whether it may interrupt them: every declaration in the repository ships
# switched off. A new one that ships on fails here before it reaches a manifest
# loader.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_every_shipped_plugin_category_ships_switched_off() -> None:
    manifests = sorted(_PLUGIN_ROOT.glob("*/meiva-plugin.json"))
    assert manifests
    for manifest_path in manifests:
        raw = json.loads(manifest_path.read_text())
        entries = cast(list[dict[str, object]], raw["contributions"].get("notifications") or [])
        errors: list[str] = []
        parsed = validate_notification_contributions(entries, errors)
        assert errors == [], manifest_path
        assert all(entry["defaultEnabled"] is False for entry in parsed), manifest_path
