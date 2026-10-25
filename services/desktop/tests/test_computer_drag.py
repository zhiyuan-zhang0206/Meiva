from __future__ import annotations

import json
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from base.db import Database
from base.host.permissions_helper.client import PermissionsHelperClient

from ..computer.config import ComputerUseConfig
from ..computer.mcp_daemon import ComputerMcpDaemon
from ..computer.tests.slices import AuditLog


class RequestLog(list[dict[str, Any]]):
    def __init__(self) -> None:
        super().__init__()
        self.error: str | None = None

    def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        self.append(request)
        method = request["method"]
        if method == "drag":
            result: dict[str, Any] = {
                "start": {"x": request["start_x"], "y": request["start_y"]},
                "end": {"x": request["end_x"], "y": request["end_y"]},
            }
        elif method == "cursor_position":
            result = {"x": -12, "y": 34}
        elif method == "scroll":
            result = {"scrolled": request["dy"]}
        elif method == "screen_size":
            result = {"w": 1000, "h": 800, "scale": 2}
        elif method == "frontmost_app":
            result = {"app": "TestTarget"}
        else:
            raise AssertionError(f"unexpected helper method {method}")
        if self.error and method == "drag":
            return {"ok": False, "error": self.error}
        return {"ok": True, "result": result}


Desktop = tuple[ComputerMcpDaemon, RequestLog, AuditLog]


@pytest.fixture
def desktop(database: Database) -> Iterator[Desktop]:
    requests = RequestLog()
    events = AuditLog(database)
    events.clear()
    errors: list[BaseException] = []
    stopped = threading.Event()
    with tempfile.TemporaryDirectory(prefix="meiva-drag-", dir="/tmp") as directory:
        path = Path(directory) / "helper.sock"
        daemon = ComputerMcpDaemon(
            ComputerUseConfig(30, 0.01, 30, 60, 1),
            database,
            sock="/unused.sock",
            helper=PermissionsHelperClient(path),
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(0.1)

            def serve() -> None:
                while not stopped.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    try:
                        with connection, connection.makefile("rb") as stream:
                            response = requests.respond(json.loads(stream.readline()))
                            connection.sendall(json.dumps(response).encode() + b"\n")
                    except BaseException as exc:
                        errors.append(exc)
                        return

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                yield daemon, requests, events
            finally:
                stopped.set()
                worker.join(timeout=3)
                assert not worker.is_alive()
                assert not errors, errors


ARGS = {"start_x": -100, "start_y": 200, "end_x": 500, "end_y": 600}


@pytest.mark.parametrize("measured_scale", [None, 1.0, 2.0])
async def test_drag_converts_both_endpoints_and_tracks_release_point(
    desktop: Desktop, measured_scale: float | None
) -> None:
    daemon, requests, _ = desktop
    daemon._scale = measured_scale
    response = await daemon._dispatch(
        {
            "id": 2,
            "method": "call_tool",
            "tool": "drag",
            "args": {**ARGS, "task_id": 42},
            "agent_id": 7,
        }
    )
    assert response["ok"] is True
    scale = measured_scale or 2.0
    request = next(req for req in requests if req["method"] == "drag")
    assert request == {
        "id": 1,
        "method": "drag",
        **{key: value / scale for key, value in ARGS.items()},
    }
    payload = cast(dict[str, Any], response["result"])
    result = json.loads(payload["content"][0]["text"])
    assert result == {
        "start": {"x": -100 / scale, "y": 200 / scale},
        "end": {"x": 500 / scale, "y": 600 / scale},
    }
    assert daemon._pointer == (500, 600)
    await daemon._dispatch(
        {"id": 3, "method": "call_tool", "tool": "scroll", "args": {"dy": 5}, "agent_id": 7}
    )
    scroll = next(req for req in requests if req["method"] == "scroll")
    assert (scroll["x"], scroll["y"]) == (-12, 34)


async def test_drag_schema_lists_required_numeric_endpoints(desktop: Desktop) -> None:
    daemon, _, _ = desktop
    listed = await daemon._dispatch({"id": 1, "method": "list_tools"})
    assert listed["ok"] is True
    tools = cast(list[dict[str, Any]], listed["result"])
    schema = next(tool["input_schema"] for tool in tools if tool["name"] == "drag")
    jsonschema.validate(ARGS, schema)
    assert schema["required"] == list(ARGS)
    for key in ARGS:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                {name: value for name, value in ARGS.items() if name != key}, schema
            )


async def test_drag_renews_screen_and_records_task_session_and_coordinates(
    desktop: Desktop,
) -> None:
    daemon, _, events = desktop
    response = await daemon._dispatch(
        {
            "id": 1,
            "method": "call_tool",
            "tool": "drag",
            "args": {**ARGS, "task_id": 42},
            "agent_id": 7,
        }
    )
    assert response["ok"] is True
    assert daemon._screen.holder == 7
    action = next(event for event in events if event.event_name == "computer_action")
    assert action.attributes["coords"] == "-100,200->500,600"
    assert action.attributes["task_id"] == 42
    assert action.attributes["outcome"] == "ok"
    assert any(event.event_name == "computer_session_start" for event in events)


@pytest.mark.parametrize("key", list(ARGS))
@pytest.mark.parametrize("invalid", [None, True, "12", float("nan"), float("inf"), 10**400])
async def test_drag_rejects_nonfinite_or_nonnumeric_coordinates(
    desktop: Desktop, key: str, invalid: Any
) -> None:
    daemon, requests, events = desktop
    response = await daemon._dispatch(
        {
            "id": 1,
            "method": "call_tool",
            "tool": "drag",
            "args": {**ARGS, key: invalid},
            "agent_id": 7,
        }
    )
    assert response["ok"] is False
    assert key in response["error"]
    assert not any(req["method"] == "drag" for req in requests)
    assert daemon._pointer is None
    assert events[-1].attributes["outcome"] == "error"


@pytest.mark.parametrize("key", list(ARGS))
async def test_drag_requires_every_endpoint_coordinate(desktop: Desktop, key: str) -> None:
    daemon, requests, _ = desktop
    response = await daemon._dispatch(
        {
            "id": 1,
            "method": "call_tool",
            "tool": "drag",
            "args": {name: value for name, value in ARGS.items() if name != key},
            "agent_id": 7,
        }
    )
    assert response["ok"] is False
    assert f"requires argument '{key}'" in response["error"]
    assert not any(req["method"] == "drag" for req in requests)


async def test_drag_permission_refusal_and_busy_screen_do_not_change_pointer(
    desktop: Desktop,
) -> None:
    daemon, requests, events = desktop
    requests.error = "Accessibility grant missing"
    response = await daemon._dispatch(
        {"id": 1, "method": "call_tool", "tool": "drag", "args": ARGS, "agent_id": 7}
    )
    assert response["ok"] is False
    assert "Accessibility grant missing" in response["error"]
    assert daemon._pointer is None
    assert events[-1].attributes["outcome"] == "error"
    requests.clear()
    response = await daemon._dispatch(
        {"id": 2, "method": "call_tool", "tool": "drag", "args": ARGS, "agent_id": 8}
    )
    assert response["ok"] is False
    assert "screen busy" in response["error"]
    assert requests == []


def test_native_dispatch_requires_accessibility_before_drag() -> None:
    source = (Path(__file__).parents[1] / "permissions_helper/helper/main.swift").read_text()
    gated = source.split("let axGatedMethods: Set<String> = [", 1)[1].split("]", 1)[0]
    assert '"drag"' in gated
    assert 'case "drag": result = try drag(req)' in source
