from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

from base.events.notifications.catalog import (
    CORE_CATEGORIES,
    NotificationCategory,
    category_from_manifest,
    merge_catalog,
)
from base.packages.plugins import enable_config
from base.packages.plugins.manifest import load_manifest


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# Core plus every enabled plugin's declaration, read from manifests like
# `ui_contributions` — nothing here imports plugin code. A plugin may only declare
# ids under its own `<plugin>.` prefix, so the merged catalog treats a duplicate
# id as a programming error. The manifests and the enabled set are parameters so
# the merge has one owner and a test can hand it a real directory.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def categories_from_manifests(
    installed: Mapping[str, Path], enabled: set[str]
) -> dict[str, NotificationCategory]:
    groups: list[list[NotificationCategory]] = [list(CORE_CATEGORIES)]
    for name in sorted(installed):
        if name not in enabled:
            continue
        manifest = load_manifest(installed[name])
        raw_notifications = manifest.contributions.get("notifications")
        declared = (
            [item for item in raw_notifications if _is_str_object_dict(item)]
            if _is_object_list(raw_notifications)
            else []
        )
        groups.append([category_from_manifest(item, plugin=name) for item in declared])
    return merge_catalog(groups)


def declared_categories() -> dict[str, NotificationCategory]:
    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    return categories_from_manifests(
        installed, {name for name, entry in config.plugins.items() if entry.enabled}
    )
