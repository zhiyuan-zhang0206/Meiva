from __future__ import annotations

from base.packages.plugins.surface.notification_contributions import (
    validate_notification_contributions,
)


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
            "defaultEnabled": True,
            "dedupeWindowSeconds": 0,
        }
    ]


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
