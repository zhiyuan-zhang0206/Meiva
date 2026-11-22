from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path

from base.clock import Clock
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.cluster.machines import list_all as list_cluster_machines
from base.cluster.machines import lookup as lookup_machine_url
from base.db import Database
from base.host.permissions_helper.client import PermissionsHelperError
from base.host.remote_hosts import (
    RemoteHostConfig,
    RemoteHostConfigError,
    ResolvedTarget,
    TargetResolutionError,
    load_remote_hosts,
    read_bearer_token,
    resolve_target,
)
from base.log import logger

from .cluster_rpc import call_cluster_computer_tool
from .ssh_tunnel import SshTunnelManager
from .win_bridge import RemoteWinBridgeHelper

ClusterToolCaller = Callable[
    [str, Mapping[str, str], str, Mapping[str, object], int | None, str, Path],
    dict[str, object],
]


def _default_cluster_caller(
    ops_url: str,
    headers: Mapping[str, str],
    tool: str,
    args: Mapping[str, object],
    agent_id: int | None,
    stamp: str,
    home: Path,
) -> dict[str, object]:
    return call_cluster_computer_tool(
        ops_url,
        headers=headers,
        tool=tool,
        args=args,
        agent_id=agent_id,
        stamp=stamp,
        home=home,
    )


class RemoteTransportRouter[HelperT]:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        home: Path,
        local_machine: str | None = None,
        gateway_access: GatewayAccess | None = None,
        user_home: Path | None = None,
        remote_helper_factory: Callable[[RemoteHostConfig, int | None], HelperT] | None = None,
        cluster_tool_caller: ClusterToolCaller | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._home = home
        self._local_machine = local_machine
        self._gateway_access = gateway_access
        self._user_home = user_home if user_home is not None else home
        self._remote_helper_factory = remote_helper_factory
        self._cluster_tool_caller = cluster_tool_caller or _default_cluster_caller
        self._tunnels = SshTunnelManager(timers=clock)
        self._ssh_helpers: dict[str, RemoteWinBridgeHelper] = {}

    def _cluster_machine_names(self) -> list[str]:
        try:
            return [name for name, _url in list_cluster_machines(self._db)]
        except Exception:
            logger.opt(exception=True).warning("[computer-mcp] failed to list cluster machines")
            return []

    def resolve(self, raw_host: object) -> ResolvedTarget:
        if raw_host is None:
            return ResolvedTarget(kind="local", name="local", canonical_uri="local")
        if not isinstance(raw_host, str):
            raise TargetResolutionError(
                f"host must be a string, got {type(raw_host).__name__}; "
                "run `meiva computer list` to view registered targets."
            )
        stripped = raw_host.strip()
        if not stripped or stripped.lower() in {"local", "local://"}:
            return ResolvedTarget(kind="local", name="local", canonical_uri="local")
        ssh_hosts = load_remote_hosts(home=self._home)
        return resolve_target(
            stripped,
            ssh_aliases=ssh_hosts.keys(),
            cluster_machines=self._cluster_machine_names(),
            local_machine=self._local_machine,
        )

    def ssh_helper_for(self, alias: str, agent_id: int | None) -> HelperT | RemoteWinBridgeHelper:
        hosts = load_remote_hosts(home=self._home)
        cfg = hosts.get(alias)
        if cfg is None:
            raise TargetResolutionError(
                f"Unknown SSH desktop host 'ssh://{alias}'. "
                "Run `meiva computer list` to view registered targets."
            )
        holder = f"meiva-agent-{agent_id or 0}"
        if self._remote_helper_factory is not None:
            return self._remote_helper_factory(cfg, agent_id)
        cached = self._ssh_helpers.get(alias)
        if cached is not None:
            cached.set_holder(holder)
            return cached
        base_url = self._tunnels.endpoint_for(cfg)
        token: str | None = None
        if cfg.token_file is not None:
            try:
                token = read_bearer_token(
                    cfg.token_file, home=self._home, user_home=self._user_home
                )
            except RemoteHostConfigError as exc:
                raise PermissionsHelperError(str(exc)) from exc
        helper = RemoteWinBridgeHelper(base_url, bearer_token=token, holder=holder)
        self._ssh_helpers[alias] = helper
        return helper

    def release_remote_ssh(self, alias: str, *, force: bool = False) -> None:
        helper = self._ssh_helpers.get(alias)
        if helper is not None:
            with suppress(PermissionsHelperError, OSError):
                _ = helper.release_remote_lease(force=force)

    def call_cluster_peer(
        self,
        machine: str,
        *,
        tool: str,
        args: dict[str, object],
        agent_id: int | None,
        stamp: str,
    ) -> dict[str, object]:
        try:
            ops_url = lookup_machine_url(self._db, machine)
        except Exception as exc:
            raise PermissionsHelperError(
                f"cluster desktop host 'meiva://{machine}' is not reachable: {exc}"
            ) from exc
        headers = (
            gateway_auth_headers(self._gateway_access) if self._gateway_access is not None else {}
        )
        return self._cluster_tool_caller(ops_url, headers, tool, args, agent_id, stamp, self._home)

    def close(self) -> None:
        self._tunnels.close_all()
