"""Console contributions — GET /api/ui/contributions.

The aggregation half of `future/frontend-plugin-contributions.md`: the console
asks one endpoint what the enabled plugins declare under `contributions.ui`,
and gets back the merged, plugin-attributed declaration set. There is no
per-plugin frontend code and no build step — a plugin's contribution is data
that the console's own components render.

**Cluster-scoped, read from manifests.** UI contributions describe the console,
which is per-cluster, so this reads the enabled set rather than any per-agent
overlay (per-agent activation, issue #39-S3, filters agent-inspect sections
only — nav and themes are cluster-level surfaces). The enabled set is this
gateway host's `plugins_config.json` until the cluster registry rows of #39-S4
land. Nothing here imports plugin code: a manifest is a file, and reading it is
all this endpoint does.

A manifest that no longer validates is a 500 naming the plugin, not a plugin
silently dropped from the list — the install gate already validated it, so a
manifest that fails here means the on-disk copy changed under the cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeGuard

from fastapi import APIRouter, HTTPException, Request

from base.packages.plugins import enable_config
from base.packages.plugins.loading.schedule_face import load_plugin_ui_stats_safe
from base.packages.plugins.manifest import ManifestError, load_manifest
from gateway.extensions.schemas import (
    UiContributionsResponse,
    UiNavContribution,
    UiStatContribution,
    UiThemeContribution,
)

router = APIRouter()


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_any_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _dict_list(value: object) -> list[dict[str, Any]]:
    return [item for item in value if _is_str_any_dict(item)] if _is_object_list(value) else []


def _str_dict(value: object) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()} if _is_str_any_dict(value) else {}


def _enabled_ui_declarations(*, home: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    installed = enable_config.installed_plugin_dirs(home=home)
    config = enable_config.load_for_runtime(set(installed), home=home)
    declarations: list[tuple[str, Path, dict[str, Any]]] = []
    for name in sorted(installed):
        plugin_dir = installed[name]
        entry = config.plugins.get(name)
        if entry is None or not entry.enabled:
            continue
        try:
            manifest = load_manifest(plugin_dir)
        except ManifestError as e:
            raise HTTPException(
                status_code=500,
                detail=f"plugin {name!r} has an invalid meiva-plugin.json: {e}",
            ) from e
        ui = manifest.contributions.get("ui")
        if not _is_str_any_dict(ui):
            continue
        declarations.append((name, plugin_dir, ui))
    return declarations


@router.get("/api/ui/contributions")
def get_ui_contributions(request: Request) -> UiContributionsResponse:
    """Every console contribution the cluster's enabled plugins declare."""
    themes: list[UiThemeContribution] = []
    nav: list[UiNavContribution] = []
    stats: list[UiStatContribution] = []
    home = request.app.state.gateway_process.home
    for plugin, plugin_dir, ui in _enabled_ui_declarations(home=home):
        for theme in _dict_list(ui.get("themes", [])):
            # Absent darkTokens is carried as None, not as an empty map: the
            # picker distinguishes "pins both modes" from "has a dark half",
            # and an empty map would read as the latter.
            dark = _str_dict(theme["darkTokens"]) if theme.get("darkTokens") is not None else None
            themes.append(
                UiThemeContribution(
                    plugin=plugin,
                    name=str(theme["name"]),
                    tokens=_str_dict(theme["tokens"]),
                    dark_tokens=dark,
                )
            )
        for entry in _dict_list(ui.get("nav", [])):
            nav.append(
                UiNavContribution(
                    plugin=plugin,
                    location=str(entry["location"]),
                    label=str(entry["label"]),
                    icon=str(entry["icon"]),
                    page=str(entry["page"]),
                )
            )
        seen_stat_ids: set[str] = set()
        for card in _dict_list(ui.get("stats", [])):
            # Declaration half only — the value half is the plugin_stats row
            # the console joins by (plugin, id) (see plugin_stats.py).
            card_id = str(card["id"])
            seen_stat_ids.add(card_id)
            stats.append(
                UiStatContribution(
                    plugin=plugin,
                    id=card_id,
                    label=str(card["label"]),
                )
            )
        stats_face = ui.get("statsFace")
        if isinstance(stats_face, str):
            for spec in load_plugin_ui_stats_safe(plugin, plugin_dir, home=home):
                if spec.id not in seen_stat_ids:
                    seen_stat_ids.add(spec.id)
                    stats.append(
                        UiStatContribution(
                            plugin=plugin,
                            id=spec.id,
                            label=spec.label,
                        )
                    )
    return UiContributionsResponse(themes=themes, nav=nav, stats=stats)
