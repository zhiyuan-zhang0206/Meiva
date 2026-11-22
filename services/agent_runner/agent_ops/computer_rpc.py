from __future__ import annotations

import asyncio
import base64
import functools
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.paths import computer_mcp_socket, logs_path
from services.agent_runner.agent_ops import in_flight

_SOCKET_TIMEOUT_S = 55.0
_MAX_IMAGE_BYTES = 24 * 1024 * 1024


def _roundtrip_computer_socket(sock_path: Path, req: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(_SOCKET_TIMEOUT_S)
        s.connect(str(sock_path))
        stream = s.makefile("rwb")
        stream.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        stream.flush()
        line = stream.readline()
    if not line:
        raise OSError("computer-mcp daemon closed the connection")
    parsed = object_of(json.loads, line)
    if not is_object_dict(parsed):
        raise ValueError("computer-mcp daemon returned non-object response")
    return parsed


def _attach_snapshot_bytes(result: dict[str, object], *, home: Path) -> None:
    raw_path = result.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return
    try:
        candidate = Path(raw_path).resolve()
        snapshots_root = (logs_path(home) / "computer" / "snapshots").resolve()
        if not candidate.is_relative_to(snapshots_root) or not candidate.is_file():
            return
        data = candidate.read_bytes()
    except OSError:
        return
    if len(data) > _MAX_IMAGE_BYTES:
        return
    result["_remote_image_b64"] = base64.b64encode(data).decode("ascii")
    result["_remote_image_ext"] = candidate.suffix or ".png"


def _unwrap_mcp_result(mcp_res: object) -> dict[str, object]:
    if not is_object_dict(mcp_res):
        return {}
    content = mcp_res.get("content")
    if not is_object_list(content) or not content:
        return {}
    first = content[0]
    if not is_object_dict(first):
        return {}
    text = first.get("text")
    if not isinstance(text, str):
        return {}
    try:
        parsed_text = object_of(json.loads, text)
    except ValueError:
        return {"text": text}
    return parsed_text if is_object_dict(parsed_text) else {"text": text}


def _json_reply(status: int, payload: dict[str, object]) -> tuple[int, bytes, str]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"


def _handle_status_rpc(sock_path: Path, os_name: str) -> tuple[int, bytes, str]:
    try:
        resp = _roundtrip_computer_socket(sock_path, {"id": 1, "method": "status"})
    except (OSError, ValueError) as exc:
        return _json_reply(
            200,
            {
                "ok": True,
                "available": False,
                "os": os_name,
                "holder": None,
                "error": f"local computer-mcp unavailable: {exc}",
            },
        )
    st = resp.get("result")
    if resp.get("ok") is not True or not is_object_dict(st):
        return _json_reply(
            200,
            {
                "ok": True,
                "available": False,
                "os": os_name,
                "holder": None,
                "error": str(resp.get("error") or "status failed"),
            },
        )
    return _json_reply(
        200,
        {
            "ok": True,
            "available": bool(st.get("local_available", False)),
            "os": os_name,
            "holder": st.get("holder"),
        },
    )


def _handle_call_tool_rpc(
    req: dict[str, object], sock_path: Path, home: Path
) -> tuple[int, bytes, str]:
    tool = req.get("tool")
    raw_args: object = req.get("args")
    if raw_args is None:
        raw_args = {}
    agent_id = req.get("agent_id")
    if not isinstance(tool, str) or not tool or not is_object_dict(raw_args):
        return _json_reply(422, {"ok": False, "error": "call_tool requires tool and object args"})
    if agent_id is not None and (not isinstance(agent_id, int) or isinstance(agent_id, bool)):
        return _json_reply(422, {"ok": False, "error": "agent_id must be an integer"})
    local_args: dict[str, object] = {k: v for k, v in raw_args.items() if k != "host"}
    try:
        resp = _roundtrip_computer_socket(
            sock_path,
            {
                "id": 1,
                "method": "call_tool",
                "tool": tool,
                "args": local_args,
                "agent_id": agent_id,
            },
        )
    except (OSError, ValueError) as exc:
        return _json_reply(
            200,
            {
                "ok": False,
                "error": f"machine has no local desktop computer-mcp service available: {exc}",
            },
        )
    if resp.get("ok") is not True:
        return _json_reply(
            200, {"ok": False, "error": str(resp.get("error") or "computer-mcp error")}
        )
    semantic = _unwrap_mcp_result(resp.get("result"))
    _attach_snapshot_bytes(semantic, home=home)
    return _json_reply(200, {"ok": True, "result": semantic})


def computer_rpc_response(
    body: bytes, *, home: Path, platform_system: str
) -> tuple[int, bytes, str]:
    try:
        req = object_of(json.loads, body)
    except ValueError:
        return _json_reply(422, {"ok": False, "error": "invalid JSON body"})
    if not is_object_dict(req):
        return _json_reply(422, {"ok": False, "error": "request body must be an object"})
    action = req.get("action")
    sock_path = computer_mcp_socket(home)
    os_name = "macos" if platform_system == "darwin" else platform_system
    if action == "status":
        return _handle_status_rpc(sock_path, os_name)
    if action == "call_tool":
        return _handle_call_tool_rpc(req, sock_path, home)
    return _json_reply(422, {"ok": False, "error": "unknown computer rpc action"})


async def computer_rpc_route(
    body: bytes,
    *,
    home: Path,
    platform_system: str,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: in_flight.RequestTokens,
    workers: in_flight.WorkerFutures,
) -> tuple[int, bytes, str]:
    with in_flight.admitted(requests=requests):
        async with dispatch_sem:
            future = asyncio.get_running_loop().run_in_executor(
                executor,
                functools.partial(
                    computer_rpc_response,
                    body,
                    home=home,
                    platform_system=platform_system,
                ),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)
