# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

from mcp.types import CallToolResult, ListToolsResult, Tool

from base.agents.context.browser_identity import hosted_browser_identity
from base.clock import Timers
from base.host.boundary.json_values import is_object_dict
from base.host.remote_hosts import load_remote_hosts, parse_target_selector, resolve_target
from base.log import logger
from base.paths import workspace_path

from .remote_files import CallerFiles
from .remote_pages import REMOTE_TOOLS, require_remote_tool
from .remote_rpc import RPC_TIMEOUT_S, call_browser_peer
from .ssh_browser import SshBrowser

type LocalRequest = Callable[[dict[str, object]], Awaitable[object]]
type PeerRequest = Callable[[str, Mapping[str, str], Mapping[str, object]], Awaitable[object]]


@dataclass(frozen=True)
class BrowserRoutes:
    home: Path
    local_machine: str | None
    local_available: Callable[[], bool]
    peers: Callable[[], Mapping[str, str | None]]
    headers: Callable[[], Mapping[str, str]]
    timers: Timers
    connect_timeout_s: float
    verify_caller: Callable[[str, str], dict[str, object]] | None = None


async def _peer_request(url: str, headers: Mapping[str, str], body: Mapping[str, object]) -> object:
    timeout = RPC_TIMEOUT_S + 5 if body.get("method") == "call_tool" else 3.0
    return await call_browser_peer(url, headers, body, timeout_s=timeout)


class RoutedBrowserSession:
    def __init__(
        self,
        routes: BrowserRoutes,
        *,
        local_request: LocalRequest,
        peer_request: PeerRequest = _peer_request,
    ) -> None:
        self.client_agent_id: int | None = None
        self.browser_caller: str | None = None
        self._routes = routes
        self._local = local_request
        self._peer = peer_request
        self._ssh: dict[tuple[str, int], SshBrowser] = {}
        self._lock = asyncio.Lock()
        self._lifetime = AsyncExitStack()
        self._tasks = asyncio.TaskGroup()

    async def start(self) -> None:
        await self._lifetime.enter_async_context(self._tasks)

    async def close(self) -> None:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self._lifetime.aclose)
            for session in self._ssh.values():
                stack.push_async_callback(session.close)
            self._ssh.clear()

    async def _local_request(self, request: dict[str, object]) -> object:
        if not self._routes.local_available():
            raise RuntimeError(
                "Local Chrome is unavailable. Run `meiva computer list` and pass "
                "host='meiva://<machine>' or host='ssh://<alias>'."
            )
        return await self._local(request)

    def _catalog(self, raw: object, *, remote: bool = False) -> ListToolsResult:
        result = ListToolsResult.model_validate({"tools": raw})
        return ListToolsResult(
            tools=[
                _with_host(tool) for tool in result.tools if not remote or tool.name in REMOTE_TOOLS
            ]
        )

    async def list_tools(self) -> ListToolsResult:
        async with self._lock:
            return await self._list_tools()

    async def _list_tools(self) -> ListToolsResult:
        if self._routes.local_available():
            with suppress(ConnectionError, RuntimeError, ValueError):
                return self._catalog(await self._local_request({"method": "list_tools"}))
        peers: Mapping[str, str | None]
        try:
            peers = await asyncio.to_thread(self._routes.peers)
        except Exception:
            logger.opt(exception=True).warning("Could not discover cluster Chrome targets")
            peers = {}
        endpoints = [
            url
            for machine, url in peers.items()
            if machine != self._routes.local_machine and url is not None
        ]
        async with asyncio.TaskGroup() as group:
            attempts = [group.create_task(self._remote_catalog(url)) for url in endpoints]
            try:
                for response in asyncio.as_completed(attempts):
                    result = await response
                    if result is not None:
                        return result
            finally:
                for attempt in attempts:
                    attempt.cancel()
        for cfg in load_remote_hosts(home=self._routes.home).values():
            async with AsyncExitStack() as stack:
                ssh = SshBrowser(
                    cfg,
                    timers=self._routes.timers,
                    tasks=self._tasks,
                    connect_timeout_s=self._routes.connect_timeout_s,
                )
                stack.push_async_callback(ssh.close)
                try:
                    tools = await ssh.list_tools()
                except (RuntimeError, OSError, ValueError):
                    continue
                return ListToolsResult(
                    tools=[_with_host(tool) for tool in tools if tool.name in REMOTE_TOOLS]
                )
        raise RuntimeError(
            "Chrome tools are unavailable. Run `meiva computer list` to discover targets."
        )

    async def _remote_catalog(self, url: str) -> ListToolsResult | None:
        try:
            return self._catalog(
                await self._peer(url, self._routes.headers(), {"method": "list_tools"}), remote=True
            )
        except (RuntimeError, OSError, ValueError):
            return None

    async def call_tool(self, name: str, args: Mapping[str, object]) -> CallToolResult:
        async with self._lock:
            return await self._call_tool(name, args)

    async def _call_tool(self, name: str, args: Mapping[str, object]) -> CallToolResult:
        arguments = dict(args)
        host = arguments.pop("host", None)
        caller = self.browser_caller
        agent_id = self.client_agent_id
        kind, _ = parse_target_selector(host)
        if kind == "local":
            return await self._call_local(name, arguments, agent_id)
        hosts = load_remote_hosts(home=self._routes.home) if kind != "meiva" else {}
        peers: Mapping[str, str | None] = (
            await asyncio.to_thread(self._routes.peers) if kind != "ssh" else {}
        )
        target = resolve_target(
            host,
            ssh_aliases=hosts,
            cluster_machines=peers,
            local_machine=self._routes.local_machine,
        )
        if target.kind == "local":
            return await self._call_local(name, arguments, agent_id)
        if agent_id is None or agent_id <= 0:
            raise ValueError("Remote Chrome requires a Meiva agent identity")
        require_remote_tool(name, arguments)
        proof = await self._issue(caller, target.canonical_uri, agent_id)
        workspace = workspace_path(self._routes.home, agent_id)
        if target.kind == "ssh":
            config = hosts[target.name]
            key = (target.name, agent_id)
            session = self._ssh.get(key)
            if session is None or session.config != config:
                if session is not None:
                    await session.close()
                session = SshBrowser(
                    config,
                    timers=self._routes.timers,
                    tasks=self._tasks,
                    connect_timeout_s=self._routes.connect_timeout_s,
                )
                self._ssh[key] = session
            return await session.call_tool(name, arguments, workspace=workspace)
        url = peers[target.name]
        if not url:
            raise RuntimeError(f"No runner endpoint for {target.canonical_uri}")
        files = CallerFiles(workspace, name, arguments)
        raw = await self._peer(
            url,
            self._routes.headers(),
            {
                "method": "call_tool",
                "tool": name,
                "args": files.args,
                "origin": self._routes.local_machine,
                "proof": proof,
                "inputs": files.inputs,
            },
        )
        if not is_object_dict(raw):
            raise ValueError("Browser peer returned an invalid tool result")
        return files.receive(CallToolResult.model_validate(raw.get("mcp")), raw.get("files"))

    async def _issue(self, caller: object, target: str, agent_id: int) -> str:
        if not isinstance(caller, str) or not caller:
            raise ValueError("Remote Chrome requires an agent-host caller credential")
        verifier = self._routes.verify_caller
        if verifier is None:
            claims = await asyncio.to_thread(
                hosted_browser_identity, self._routes.home, "issue", caller, target
            )
        else:
            claims = verifier(caller, target)
        proof = claims.get("proof")
        if claims.get("agent_id") != agent_id or not isinstance(proof, str):
            raise ValueError("Chrome caller credential does not belong to this agent")
        return proof

    async def _call_local(
        self,
        name: str,
        args: dict[str, object],
        agent_id: int | None,
    ) -> CallToolResult:
        request: dict[str, object] = {"method": "call_tool", "tool": name, "args": args}
        if agent_id is not None:
            request["agent_id"] = agent_id
        return CallToolResult.model_validate(await self._local_request(request))


def _with_host(tool: Tool) -> Tool:
    if tool.name not in REMOTE_TOOLS:
        return tool
    schema = dict(tool.input_schema)
    properties = schema.get("properties")
    schema["properties"] = {
        **(properties if is_object_dict(properties) else {}),
        "host": {
            "type": "string",
            "description": "Chrome target. Omit for local, or use meiva://<machine> or ssh://<alias>.",
        },
    }
    return tool.model_copy(update={"input_schema": schema})
