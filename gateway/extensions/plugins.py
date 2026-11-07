# meiva:link gateway/extensions/docs/inventory.meiva.okf.md

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, StrictBool

from base.native_process.os_platform import file_lock
from base.packages.extensions import plugin_enable, registry
from base.packages.plugins import enable_config
from gateway.http.config import gateway_state

router = APIRouter()


class ClusterPluginView(BaseModel):
    name: str
    enabled: bool
    description: str


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


def _descriptions(*, home: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, root in enable_config.discover_plugins(home=home).items():
        result[name] = enable_config.parse_description(root / "plugin.py")
    return result


@router.get("/api/plugins")
def get_plugins(request: Request) -> ClusterPluginsView:
    state = gateway_state(request)
    home = state.gateway_process.home
    descriptions = _descriptions(home=home)
    local = enable_config.load_for_runtime(set(descriptions), home=home)
    with state.db_pool.connection() as conn:
        policy = plugin_enable.policy(conn)
    return ClusterPluginsView(
        plugins=[
            ClusterPluginView(
                name=name,
                enabled=policy[name] if name in policy else local.plugins[name].enabled,
                description=descriptions.get(name, ""),
            )
            for name in sorted(set(descriptions) | policy.keys())
        ]
    )


@router.put("/api/plugins")
def put_plugin(body: ClusterPluginWrite, request: Request) -> ClusterPluginSaved:
    state = gateway_state(request)
    home = state.gateway_process.home
    with file_lock(home / "plugin_enable_policy.lock", timeout_s=10):
        try:
            with state.db_pool.connection() as conn:
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
