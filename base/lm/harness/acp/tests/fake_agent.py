"""A scripted ACP agent: enough of the wire to drive the transport in tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

_PENDING: dict[str, dict[str, Any]] = {}
_PERMISSION_WAIT: dict[int, str] = {}
_OPTIONS = [
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]


def _send(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _update(session_id: str, update: dict[str, Any]) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _text(prompt: list[dict[str, Any]]) -> str:
    return "".join(str(block.get("text", "")) for block in prompt if block.get("type") == "text")


def _initialize(frame: dict[str, Any]) -> None:
    _result(
        frame.get("id"),
        {
            "protocolVersion": 1,
            "agentInfo": {"name": "fake-acp-agent", "version": "0.0.1"},
            "agentCapabilities": {"sessionCapabilities": {"list": {}, "resume": {}, "close": {}}},
            "authMethods": [],
        },
    )


def _new_session(frame: dict[str, Any]) -> None:
    _result(
        frame.get("id"),
        {
            "sessionId": "session-1",
            "configOptions": [
                {
                    "id": "model",
                    "name": "Model",
                    "type": "select",
                    "currentValue": "m1",
                    "options": [{"value": "m1", "name": "m1"}],
                }
            ],
        },
    )


def _resume(frame: dict[str, Any]) -> None:
    _result(frame.get("id"), {"configOptions": []})


def _close(frame: dict[str, Any]) -> None:
    _result(frame.get("id"), {})


def _cancel(frame: dict[str, Any]) -> None:
    params = frame.get("params") or {}
    pending = _PENDING.pop(str(params.get("sessionId", "")), None)
    if pending is not None:
        _result(pending["id"], {"stopReason": "cancelled"})


def _prompt(frame: dict[str, Any]) -> None:
    params = frame.get("params") or {}
    session_id = str(params.get("sessionId", ""))
    text = _text(params.get("prompt") or [])
    if "permission" in text:
        _ask_permission(frame, session_id)
        return
    if "slow" in text:
        _PENDING[session_id] = {"id": frame.get("id")}
        return
    _update(
        session_id,
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}},
    )
    _update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"echo:{text}"},
        },
    )
    _update(session_id, {"sessionUpdate": "usage_update", "used": 10, "size": 1000})
    _result(frame.get("id"), {"stopReason": "end_turn"})


def _ask_permission(frame: dict[str, Any], session_id: str) -> None:
    _PENDING[session_id] = {"id": frame.get("id")}
    _PERMISSION_WAIT[900] = session_id
    _send(
        {
            "jsonrpc": "2.0",
            "id": 900,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "call-1", "title": "bash"},
                "options": _OPTIONS,
            },
        }
    )


def _response(frame: dict[str, Any]) -> None:
    session_id = _PERMISSION_WAIT.pop(frame.get("id"), None)
    if session_id is None:
        return
    outcome = (frame.get("result") or {}).get("outcome") or {}
    chosen = outcome.get("optionId") or outcome.get("outcome")
    _update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"decision:{chosen}"},
        },
    )
    pending = _PENDING.pop(session_id, None)
    if pending is not None:
        _result(pending["id"], {"stopReason": "end_turn"})


_HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "initialize": _initialize,
    "session/new": _new_session,
    "session/resume": _resume,
    "session/close": _close,
    "session/prompt": _prompt,
    "session/cancel": _cancel,
}


def _handle(frame: dict[str, Any]) -> None:
    method = frame.get("method")
    if method is None:
        _response(frame)
        return
    handler = _HANDLERS.get(str(method))
    if handler is not None:
        handler(frame)
        return
    if frame.get("id") is not None:
        _error(frame.get("id"), -32601, f"fake agent has no method {method}")


def main() -> None:
    if "--garbage" in sys.argv:
        sys.stdout.write("this is not a protocol frame\n")
        sys.stdout.flush()
    for line in sys.stdin:
        stripped = line.strip()
        if stripped:
            _handle(json.loads(stripped))


if __name__ == "__main__":
    main()
