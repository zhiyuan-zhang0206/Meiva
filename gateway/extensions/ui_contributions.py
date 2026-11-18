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

from fastapi import APIRouter, HTTPException, Request

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.packages.plugins.loading.ui_declarations import (
    InvalidPluginManifestError,
    enabled_plugin_ui,
    stat_cards,
)
from gateway.extensions.schemas import (
    UiContributionsResponse,
    UiNavContribution,
    UiStatContribution,
    UiThemeContribution,
)
from gateway.http.config import gateway_state

router = APIRouter()


def _dict_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if is_object_dict(item)] if is_object_list(value) else []


def _str_dict(value: object) -> dict[str, str]:
    return {k: str(v) for k, v in value.items()} if is_object_dict(value) else {}


@router.get("/api/ui/contributions")
def get_ui_contributions(request: Request) -> UiContributionsResponse:
    """Every console contribution the cluster's enabled plugins declare."""
    themes: list[UiThemeContribution] = []
    nav: list[UiNavContribution] = []
    stats: list[UiStatContribution] = []
    home = gateway_state(request).gateway_process.home
    try:
        declarations = enabled_plugin_ui(home=home)
    except InvalidPluginManifestError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    for declaration in declarations:
        plugin, ui = declaration.plugin, declaration.ui
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
        stats.extend(
            UiStatContribution(plugin=card.plugin, id=card.id, label=card.label)
            for card in stat_cards(declaration, home=home)
        )
    return UiContributionsResponse(themes=themes, nav=nav, stats=stats)
