# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from base.agents.context.browser_identity import hosted_browser_identity
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.cluster.machines import lookup as lookup_machine_url
from base.daemon.http_transport import ServedRoute
from base.db import Database
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.remote_hosts import parse_target_selector
from base.paths import chrome_mcp_socket
from services.desktop.browser.routing.remote_files import MAX_RPC_BYTES
from services.desktop.browser.routing.remote_pages import require_remote_tool
from services.desktop.browser.routing.remote_rpc import (
    BROWSER_PROOF_ROUTE,
    BROWSER_RPC_ROUTE,
    call_browser_peer,
    call_browser_socket,
)

from .computer_rpc import computer_rpc_route
from .in_flight import RequestTokens, WorkerFutures, admitted

type CallerProof = Callable[[str, str], Awaitable[object]]


class BrowserRpc:
    def __init__(self, sock: Path, *, requests: RequestTokens, verify: CallerProof) -> None:
        self._socket = sock
        self._requests = requests
        self._verify = verify

    async def _tool(self, req: dict[str, object]) -> object:
        tool, args = req.get("tool"), req.get("args")
        origin, proof = req.get("origin"), req.get("proof")
        if not isinstance(tool, str) or not tool or not is_object_dict(args):
            raise ValueError("Browser RPC requires a tool name and object arguments")
        if "agent_id" in req or not isinstance(origin, str) or not isinstance(proof, str):
            raise ValueError("Browser RPC requires a target-bound caller proof")
        inputs = req.get("inputs", {})
        if not is_object_dict(inputs) or "host" in args:
            raise ValueError("Invalid remote browser file data or nested host")
        require_remote_tool(tool, args)
        agent_id = await self._verify(origin, proof)
        if type(agent_id) is not int or agent_id <= 0:
            raise ValueError("Remote Chrome caller proof did not yield an agent identity")
        return await call_browser_socket(
            self._socket,
            {
                "method": "call_remote_tool",
                "tool": tool,
                "args": args,
                "inputs": inputs,
                "agent_id": agent_id,
            },
        )

    async def _dispatch(self, req: dict[str, object]) -> object:
        method = req.get("method")
        if method == "status":
            try:
                return (
                    await call_browser_socket(self._socket, {"method": "status"}, timeout_s=1.0)
                    is True
                )
            except (OSError, RuntimeError, ValueError):
                return False
        if method == "list_tools":
            return await call_browser_socket(
                self._socket, {"method": "list_remote_tools"}, timeout_s=2.0
            )
        if method == "call_tool":
            return await self._tool(req)
        raise ValueError("Unknown browser RPC method")

    async def route(self, body: bytes) -> tuple[int, bytes, str]:
        try:
            req = object_of(json.loads, body)
            with admitted(requests=self._requests):
                result = await self._dispatch(req if is_object_dict(req) else {})
        except (OSError, RuntimeError, ValueError) as exc:
            return _reply({"ok": False, "error": str(exc)})
        return _reply({"ok": True, "result": result})


async def redeem_browser_proof(body: bytes, *, home: Path) -> tuple[int, bytes, str]:
    try:
        req = object_of(json.loads, body)
        if not is_object_dict(req):
            return _reply({"ok": False, "error": "Invalid Chrome proof request"})
        proof, target = req.get("proof"), req.get("target")
        if not isinstance(proof, str) or not isinstance(target, str):
            return _reply({"ok": False, "error": "Invalid Chrome proof request"})
        claims = await asyncio.to_thread(hosted_browser_identity, home, "redeem", proof, target)
    except (OSError, RuntimeError, ValueError):
        return _reply({"ok": False, "error": "The source agent-host rejected the Chrome proof"})
    return _reply({"ok": True, "result": claims.get("agent_id")})


def _reply(value: dict[str, object]) -> tuple[int, bytes, str]:
    return 200, json.dumps(value, ensure_ascii=False).encode(), "application/json"


async def verify_browser_peer(
    database: Callable[[], Database], access: GatewayAccess, target: str, origin: str, proof: str
) -> object:
    kind, source = parse_target_selector(origin)
    if kind != "short":
        raise ValueError("Malformed Chrome proof issuer")
    url = await asyncio.to_thread(lambda: lookup_machine_url(database(), source))
    return await call_browser_peer(
        url,
        gateway_auth_headers(access),
        {"proof": proof, "target": target},
        timeout_s=5.0,
        proof_request=True,
    )


@dataclass(frozen=True)
class DesktopRoutes:
    routes: dict[tuple[str, str], ServedRoute]
    limits: dict[tuple[str, str], int]
    slots: dict[tuple[str, str], asyncio.Semaphore]


def desktop_routes(
    home: Path,
    cdp_port: int,
    machine: str,
    *,
    database: Callable[[], Database],
    access: GatewayAccess,
    platform_system: str,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: RequestTokens,
    workers: WorkerFutures,
) -> DesktopRoutes:
    browser = BrowserRpc(
        chrome_mcp_socket(home, cdp_port),
        requests=requests,
        verify=partial(verify_browser_peer, database, access, f"meiva://{machine}"),
    )
    return DesktopRoutes(
        routes={
            BROWSER_RPC_ROUTE: browser.route,
            BROWSER_PROOF_ROUTE: partial(redeem_browser_proof, home=home),
            ("POST", "/computer/rpc"): partial(
                computer_rpc_route,
                home=home,
                platform_system=platform_system,
                executor=executor,
                dispatch_sem=dispatch_sem,
                requests=requests,
                workers=workers,
            ),
        },
        limits={BROWSER_RPC_ROUTE: MAX_RPC_BYTES},
        slots={BROWSER_RPC_ROUTE: asyncio.Semaphore(2), BROWSER_PROOF_ROUTE: asyncio.Semaphore(8)},
    )
