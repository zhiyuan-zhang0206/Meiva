from __future__ import annotations

from typing import Any, cast

IMPORTANCES = ("low", "normal", "high")

_FIELDS = frozenset(
    {"id", "label", "description", "importance", "defaultEnabled", "dedupeWindowSeconds"}
)


def _string(
    entry: dict[str, Any], field: str, where: str, errors: list[str], *, default: str | None = None
) -> str | None:
    value = entry.get(field, default)
    if not isinstance(value, str):
        errors.append(f"{where}.{field} must be a string")
        return None
    if field == "label" and not value.strip():
        errors.append(f"{where}.label must be a non-empty string")
        return None
    return value


def _optional_fields(
    entry: dict[str, Any], where: str, errors: list[str]
) -> dict[str, object] | None:
    importance = entry.get("importance", "normal")
    if importance not in IMPORTANCES:
        errors.append(f"{where}.importance must be one of {', '.join(IMPORTANCES)}")
        return None
    default_enabled = entry.get("defaultEnabled", True)
    if not isinstance(default_enabled, bool):
        errors.append(f"{where}.defaultEnabled must be a boolean")
        return None
    window = entry.get("dedupeWindowSeconds", 0)
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        errors.append(f"{where}.dedupeWindowSeconds must be a non-negative integer")
        return None
    return {
        "importance": importance,
        "defaultEnabled": default_enabled,
        "dedupeWindowSeconds": window,
    }


# meiva:note
# `contributions.notifications` is declaration only: the manifest names a category,
# and the gateway's notification spine owns the switches, the dedupe window and the
# delivery log. A duplicate id inside one manifest is a validator error here; an id
# outside the plugin's own `<plugin>.` namespace is refused when the gateway merges
# the catalog (`gateway/extensions/notification_categories.py:declared_categories`),
# never at delivery time.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md
def validate_notification_contributions(value: Any, errors: list[str]) -> list[dict[str, object]]:
    if not isinstance(value, list):
        errors.append("contributions.notifications must be a list")
        return []
    parsed: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(cast(list[Any], value)):
        where = f"contributions.notifications[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where} must be an object")
            continue
        entry = cast(dict[str, Any], raw)
        unknown = sorted(set(entry) - _FIELDS)
        if unknown:
            errors.append(f"{where}: unknown field {unknown[0]!r}")
            continue
        category_id = entry.get("id")
        if not isinstance(category_id, str) or "." not in category_id:
            errors.append(f"{where}.id must be namespaced as '<plugin>.<event>'")
            continue
        if category_id in seen:
            errors.append(f"{where}: duplicate id {category_id!r}")
            continue
        label = _string(entry, "label", where, errors)
        description = _string(entry, "description", where, errors, default="")
        optional = _optional_fields(entry, where, errors)
        if label is None or description is None or optional is None:
            continue
        seen.add(category_id)
        parsed.append({"id": category_id, "label": label, "description": description, **optional})
    return parsed
