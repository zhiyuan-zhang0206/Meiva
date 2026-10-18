from __future__ import annotations

from typing import Any, cast

from base.events.notifications.catalog import CORE_CATEGORIES, NotificationCategory, merge_catalog
from base.packages.plugins import enable_config
from base.packages.plugins.manifest import load_manifest


# meiva:note
# Core plus every enabled plugin's declaration, read from manifests like
# `ui_contributions` — nothing here imports plugin code. A plugin may only declare
# ids under its own `<plugin>.` prefix, so the merged catalog treats a duplicate
# id as a programming error.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def declared_categories() -> dict[str, NotificationCategory]:
    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    groups: list[list[NotificationCategory]] = [list(CORE_CATEGORIES)]
    for name in sorted(installed):
        entry = config.plugins.get(name)
        if entry is None or not entry.enabled:
            continue
        manifest = load_manifest(installed[name])
        declared = cast(list[dict[str, Any]], manifest.contributions.get("notifications") or [])
        categories: list[NotificationCategory] = []
        for item in declared:
            category = NotificationCategory(
                id=item["id"],
                label=item["label"],
                description=item["description"],
                importance=item["importance"],
                default_enabled=item["defaultEnabled"],
                dedupe_window_seconds=item["dedupeWindowSeconds"],
            )
            if not category.id.startswith(f"{name}."):
                raise ValueError(
                    f"plugin {name!r} declared {category.id!r} outside its own namespace"
                )
            categories.append(category)
        groups.append(categories)
    return merge_catalog(groups)
