# meiva:link services/desktop/browser/docs/operations.md

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.native_process.root_control.client import RootClient, native_identity, peer_pid
from base.paths import root_run_dir, run_dir

BROWSER_CALLER_ENV = "MEIVA_BROWSER_CALLER"
PURPOSE = "chrome.call_tool"
_PROOF_TTL_S = 30
_PROOF_LIMIT = 256
_PRIVATE_SOCKET_NAME = "chrome-caller.sock"


@dataclass(frozen=True)
class _Proof:
    agent_id: int
    caller: str
    target: str
    purpose: str
    expires: float


class BrowserAuthority:
    def __init__(self, *, new_token: Callable[[], str], now: Callable[[], float]) -> None:
        self._new_token = new_token
        self._now = now
        self._callers: dict[str, int] = {}
        self._proofs: dict[str, _Proof] = {}

    def new_caller(self, agent_id: int) -> str:
        token = self._new_token()
        self.register(agent_id, token)
        return token

    def register(self, agent_id: int, token: str) -> None:
        self._callers[token] = agent_id

    def revoke(self, token: str) -> None:
        _ = self._callers.pop(token, None)

    def issue(self, caller: str, target: str, purpose: str) -> dict[str, object]:
        agent_id = self._callers.get(caller)
        if agent_id is None or purpose != PURPOSE:
            raise ValueError("Chrome caller credential is invalid or expired")
        now = self._now()
        self._proofs = {key: proof for key, proof in self._proofs.items() if proof.expires > now}
        if len(self._proofs) >= _PROOF_LIMIT:
            raise ValueError("Chrome proof issuer is busy")
        token = self._new_token()
        self._proofs[token] = _Proof(agent_id, caller, target, purpose, now + _PROOF_TTL_S)
        return {"agent_id": agent_id, "proof": token}

    def redeem(self, token: str, target: str, purpose: str) -> int:
        proof = self._proofs.get(token)
        if (
            proof is None
            or proof.expires <= self._now()
            or proof.target != target
            or proof.purpose != purpose
            or self._callers.get(proof.caller) != proof.agent_id
        ):
            raise ValueError("Chrome proof is invalid, expired or bound to another target")
        del self._proofs[token]
        return proof.agent_id

    def dispatch(self, request: object) -> dict[str, object]:
        if not is_object_dict(request):
            raise TypeError("Invalid Chrome identity request")
        token, target, purpose = request.get("token"), request.get("target"), request.get("purpose")
        if (
            not isinstance(token, str)
            or not isinstance(target, str)
            or not isinstance(purpose, str)
        ):
            raise TypeError("Invalid Chrome identity request")
        match request.get("method"):
            case "issue":
                return self.issue(token, target, purpose)
            case "redeem":
                return {"agent_id": self.redeem(token, target, purpose)}
            case _:
                raise ValueError("Unknown Chrome identity method")


async def serve_browser_authority(home: Path, authority: BrowserAuthority) -> asyncio.Server:
    path = run_dir(home) / _PRIVATE_SOCKET_NAME
    path.unlink(missing_ok=True)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 2.0)
                result = authority.dispatch(object_of(json.loads, line))
                response = {"ok": True, **result}
            except (TimeoutError, TypeError, ValueError):
                response = {"ok": False}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, path=path, limit=4096)


def hosted_browser_identity(home: Path, method: str, token: str, target: str) -> dict[str, object]:
    root = RootClient(root_run_dir(home) / "meiva-root.sock", timeout=2).status()
    result = root.get("result")
    units = result.get("units") if is_object_dict(result) else None
    if root.get("ok") is not True or not is_object_list(units):
        raise ValueError("Chrome identity requires a supervised agent-host")
    host = next(
        (unit for unit in units if is_object_dict(unit) and unit.get("id") == "agent-host"), None
    )
    if not is_object_dict(host):
        raise ValueError("Chrome identity requires a supervised agent-host")
    identity = native_identity(
        {
            "pid": host.get("pid"),
            "create_time": host.get("create_time"),
            "starttime": host.get("starttime"),
        }
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(2)
        channel.connect(str(run_dir(home) / _PRIVATE_SOCKET_NAME))
        if peer_pid(channel) != identity.pid or not identity.live():
            raise ValueError("Chrome identity socket does not belong to the supervised agent-host")
        packet = {"method": method, "token": token, "target": target, "purpose": PURPOSE}
        channel.sendall(json.dumps(packet).encode() + b"\n")
        with channel.makefile("rb") as stream:
            response = object_of(json.loads, stream.readline(4096))
    if not is_object_dict(response) or response.get("ok") is not True:
        raise ValueError("Chrome caller credential or proof rejected by agent-host")
    return response
