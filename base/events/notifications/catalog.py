# meiva:note
# A notification is a request that needs the user's reply. An event the user does
# not have to answer — a finished turn, a closed task — belongs in the log, the
# task board or the inbox that already carries it, never in this catalog.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Importance = Literal["low", "normal", "high"]
IMPORTANCES: frozenset[str] = frozenset({"low", "normal", "high"})


@dataclass(frozen=True)
class NotificationCategory:
    id: str
    label: str
    description: str
    importance: Importance
    default_enabled: bool
    dedupe_window_seconds: int = 0

    def __post_init__(self) -> None:
        if not self.id or "." not in self.id:
            raise ValueError(f"notification category id must be namespaced: {self.id!r}")
        if self.importance not in IMPORTANCES:
            raise ValueError(f"unknown notification importance: {self.importance!r}")
        if self.dedupe_window_seconds < 0:
            raise ValueError(f"dedupe window must be >= 0: {self.id}")
        if not self.label.strip():
            raise ValueError(f"notification category {self.id} needs a label")


# meiva:note
# Core ids are `core.`-prefixed and a plugin contributes only `<plugin>.` ids, so
# the merged catalog can reject a duplicate id as a programming error rather than
# resolve an ownership clash at delivery time. The catalog holds requests, not
# events: `core.needs_input` is the only thing the user has to answer, so it is
# the only core category. A finished turn or a closed task is a log line.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
CORE_CATEGORIES: tuple[NotificationCategory, ...] = (
    NotificationCategory(
        id="core.needs_input",
        label="Agent is waiting on your answer",
        description="An agent posted a notice that needs a response.",
        importance="high",
        default_enabled=True,
        dedupe_window_seconds=3600,
    ),
)


def merge_catalog(
    groups: Iterable[Iterable[NotificationCategory]],
) -> dict[str, NotificationCategory]:
    merged: dict[str, NotificationCategory] = {}
    for group in groups:
        for category in group:
            if category.id in merged:
                raise ValueError(f"notification category declared twice: {category.id}")
            merged[category.id] = category
    return merged


def core_catalog() -> dict[str, NotificationCategory]:
    return {category.id: category for category in CORE_CATEGORIES}


# meiva:note
# One field of a plugin's manifest entry as the type the category needs. The manifest validator checked
# the entry's shape when the plugin loaded; a field of another type here is a manifest that was not
# validated, and it stops with the field's name. A missing key is a `KeyError`.
def _text(entry: Mapping[str, object], key: str) -> str:
    value = entry[key]
    if not isinstance(value, str):
        raise TypeError(f"notification category {key} must be a string, got {type(value).__name__}")
    return value


def _flag(entry: Mapping[str, object], key: str) -> bool:
    value = entry[key]
    if not isinstance(value, bool):
        raise TypeError(
            f"notification category {key} must be a boolean, got {type(value).__name__}"
        )
    return value


def _whole(entry: Mapping[str, object], key: str) -> int:
    value = entry[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"notification category {key} must be an integer, got {type(value).__name__}"
        )
    return value


def _importance(entry: Mapping[str, object]) -> Importance:
    value = entry["importance"]
    if value == "low":
        return "low"
    if value == "normal":
        return "normal"
    if value == "high":
        return "high"
    raise ValueError(f"unknown notification importance: {value!r}")


# meiva:note
# One owner for the manifest-to-category mapping: the gateway reads plugins to serve
# the switch list, and the CLI reads the same manifests to replay their categories, so
# the field names and the `<plugin>.` namespace rule may not be spelled twice.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def category_from_manifest(entry: Mapping[str, object], *, plugin: str) -> NotificationCategory:
    category = NotificationCategory(
        id=_text(entry, "id"),
        label=_text(entry, "label"),
        description=_text(entry, "description"),
        importance=_importance(entry),
        default_enabled=_flag(entry, "defaultEnabled"),
        dedupe_window_seconds=_whole(entry, "dedupeWindowSeconds"),
    )
    if not category.id.startswith(f"{plugin}."):
        raise ValueError(f"plugin {plugin!r} declared {category.id!r} outside its own namespace")
    return category
