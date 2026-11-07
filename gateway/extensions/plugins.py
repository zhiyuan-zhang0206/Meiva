# meiva:link gateway/extensions/docs/inventory.meiva.okf.md

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, StrictBool

from base.packages.extensions import plugin_enable, registry
from base.packages.plugins import enable_config
from base.packages.plugins.manifest import (
    PluginCategory,
    PluginManifest,
    load_optional_manifest,
    validate,
)
from base.packages.skills.names import find
from gateway.http.config import gateway_state

router = APIRouter()


class ClusterPluginView(BaseModel):
    name: str
    enabled: bool
    description: str
    display_name: str | None = None
    user_toggleable: bool = True
    category: PluginCategory = "extension"
    provider_kinds: list[Literal["llm", "decision"]] = Field(default_factory=list)


class ClusterPluginsView(BaseModel):
    plugins: list[ClusterPluginView]


class ClusterPluginWrite(BaseModel):
    name: str
    enabled: StrictBool


class ClusterPluginSaved(BaseModel):
    name: str
    enabled: bool
    restart_required: bool


def initialize_plugins(pool: ConnectionPool, *, home: Path) -> None:
    with pool.connection() as conn:
        plugin_enable.initialize(conn, home=home)


def _plugin_view(
    name: str, *, enabled: bool, manifest: PluginManifest | None, description: str
) -> ClusterPluginView:
    kinds: list[Literal["llm", "decision"]] = []
    if manifest is not None:
        if manifest.contributions.get("providers"):
            kinds.append("llm")
        if manifest.contributions.get("decisionProviders"):
            kinds.append("decision")
    return ClusterPluginView(
        name=name,
        enabled=enabled,
        description=description,
        display_name=manifest.display_name if manifest else None,
        user_toggleable=manifest.user_toggleable if manifest else True,
        provider_kinds=kinds,
        category=manifest.category if manifest else "extension",
    )


def _manifests(
    roots: dict[str, Path], registered: list[registry.Extension]
) -> dict[str, PluginManifest | None]:
    manifests = {
        ext.name: validate(ext.manifest) if ext.manifest is not None else None for ext in registered
    }
    for name, root in roots.items():
        manifests[name] = load_optional_manifest(root)
    return manifests


@router.get("/api/plugins")
def get_plugins(request: Request) -> ClusterPluginsView:
    state = gateway_state(request)
    home = state.gateway_process.home
    roots = enable_config.discover_plugins(home=home)
    local = enable_config.load_for_runtime(set(roots), home=home)
    with state.db_pool.connection() as conn:
        registered = registry.list_all(conn, kind="plugin")
    policy = {ext.name: ext.default_enabled for ext in registered}
    manifests = _manifests(roots, registered)
    return ClusterPluginsView(
        plugins=[
            _plugin_view(
                name,
                enabled=policy[name] if name in policy else local.plugins[name].enabled,
                manifest=manifests[name],
                description=enable_config.parse_description(roots[name] / "plugin.py")
                if name in roots
                else "",
            )
            for name in sorted(manifests)
        ]
    )


@router.put("/api/plugins")
def put_plugin(body: ClusterPluginWrite, request: Request) -> ClusterPluginSaved:
    state = gateway_state(request)
    home = state.gateway_process.home
    try:
        with state.db_pool.connection() as conn:
            roots = enable_config.discover_plugins(home=home)
            manifests = _manifests(roots, registry.list_all(conn, kind="plugin"))
            resolved = find(body.name, set(manifests))
            manifest = manifests.get(resolved) if resolved is not None else None
            if manifest is not None and not manifest.user_toggleable:
                raise HTTPException(
                    status_code=403,
                    detail=f"Plugin {resolved!r} does not allow Settings toggles",
                )
            name = plugin_enable.set_enabled(conn, body.name, enabled=body.enabled, home=home)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except registry.ExtensionTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    try:
        with state.db_pool.connection() as conn:
            plugin_enable.materialize(conn, home=home)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cluster setting saved; gateway cache refresh failed: {exc}",
        ) from exc

    return ClusterPluginSaved(name=name, enabled=body.enabled, restart_required=True)
