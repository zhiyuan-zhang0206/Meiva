from __future__ import annotations

import base64
import io
import json
import threading
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from PIL import Image

from base.clock.tests.fakes import system_clock
from base.clock.tests.manual_timers import ManualTimers
from base.db import Database
from base.host.boundary.json_values import is_object_dict
from base.host.permissions_helper.client import PermissionsHelperError
from base.host.permissions_helper.wire import ClickResult, FrontmostApp, ScreenSize
from base.host.remote_hosts import RemoteHostConfig
from services.desktop.computer import execute as exec_mod
from services.desktop.computer import mcp_daemon
from services.desktop.computer.tests.slices import (
    AuditLog,
    FakeDesktopHelper,
    computer_use_config,
    text_block,
)
from services.desktop.remote import (
    CORNER_FAILSAFE_CODE,
    CornerFailsafeError,
    RemoteTransportRouter,
    RemoteWinBridgeHelper,
    call_cluster_computer_tool,
    extract_mcp_tool_payload,
    macos_keycode_to_win_key,
    parse_streamable_http_body,
    ssh_tunnel_argv,
    win_uia_to_ax_tree,
)


def _make_webp_bytes(width: int = 320, height: int = 180) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 60)).save(buf, format="WEBP")
    return buf.getvalue()


def _make_png_bytes(width: int = 200, height: int = 100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_every_mcp_tool_exposes_optional_host_parameter() -> None:
    for tool in exec_mod.TOOLS:
        schema = tool["input_schema"]
        assert is_object_dict(schema)
        props = schema["properties"]
        assert is_object_dict(props)
        assert "host" in props, f"tool {tool['name']} missing optional host parameter"


def test_remote_plan_pure_helpers() -> None:
    sse = (
        b"event: message\r\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"ok\\":true,\\"x\\":1}"}],"isError":false}}\r\n\r\n'
    )
    parsed = parse_streamable_http_body(sse, "text/event-stream")
    out = extract_mcp_tool_payload(parsed)
    assert out.data == {"ok": True, "x": 1}
    assert out.image_bytes is None
    assert out.mime_type is None

    abort_rpc: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ok": False,
                            "code": CORNER_FAILSAFE_CODE,
                            "message": "cursor entered top-left corner",
                        }
                    ),
                }
            ],
            "isError": False,
        },
    }
    abort_out = extract_mcp_tool_payload(abort_rpc)
    assert abort_out.error_code == CORNER_FAILSAFE_CODE
    assert "corner" in (abort_out.error_message or "")

    assert macos_keycode_to_win_key(36) == "Enter"
    assert macos_keycode_to_win_key(1, modifiers=["ctrl", "shift"]) == "Ctrl+Shift+s"
    assert ssh_tunnel_argv("zzyasus", local_port=19999, remote_port=18792) == [
        "ssh",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        "127.0.0.1:19999:127.0.0.1:18792",
        "--",
        "zzyasus",
    ]

    ax, id_map = win_uia_to_ax_tree(
        {
            "window": {"hwnd": 100, "pid": 42, "process": "CC4.exe", "title": "Character Creator"},
            "elements": [
                {
                    "id": "e1",
                    "role": "Window",
                    "name": "Character Creator",
                    "rect": [0, 0, 1920, 1080],
                },
                {
                    "id": "e2",
                    "role": "Button",
                    "name": "Export",
                    "automation_id": "btnExport",
                    "rect": [100, 50, 80, 30],
                    "patterns": ["Invoke"],
                },
            ],
            "truncated": False,
            "elapsed_ms": 12,
        }
    )
    assert ax["app"] == "CC4.exe"
    assert ax["pid"] == 42
    node1 = ax["nodes"][1]
    assert node1.get("role") == "AXButton"
    assert node1.get("title") == "Export"
    assert id_map[2] == "e2"


def test_remote_win_bridge_helper_end_to_end_over_mock_http(tmp_path: Path) -> None:
    webp = _make_webp_bytes(640, 360)
    calls: list[tuple[str, dict[str, object]]] = []
    aborted = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "session": {"pid": 10, "session_id": 1, "desktop_interactive": True},
                    "session_aborted": aborted["flag"],
                    "overlay_visible": True,
                    "lease_holder": "meiva-agent-7",
                },
            )
        assert request.headers.get("authorization") == "Bearer test-bearer-value"
        body = json.loads(request.content)
        tool = str(body["params"]["name"])
        args = dict(body["params"]["arguments"])
        calls.append((tool, args))

        def sse_reply(content: list[dict[str, object]]) -> httpx.Response:
            rpc = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": content, "isError": False},
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"event: message\r\ndata: {json.dumps(rpc)}\r\n\r\n".encode(),
            )

        if tool == "status":
            return sse_reply(
                [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": True,
                                "screen": {
                                    "monitors": [
                                        {
                                            "index": 1,
                                            "role": "primary",
                                            "left": 0,
                                            "top": 0,
                                            "width": 640,
                                            "height": 360,
                                        }
                                    ]
                                },
                                "foreground_window": {"process": "Blender.exe", "title": "Blender"},
                                "session": {"desktop_interactive": True},
                            }
                        ),
                    }
                ]
            )
        if tool == "screenshot":
            meta = {
                "ok": True,
                "capture_rect": [0, 0, 640, 360],
                "image_size": [640, 360],
                "scale_x": 1.0,
                "scale_y": 1.0,
                "mime_type": "image/webp",
                "bytes": len(webp),
            }
            return sse_reply(
                [
                    {"type": "text", "text": json.dumps(meta)},
                    {
                        "type": "image",
                        "data": base64.b64encode(webp).decode("ascii"),
                        "mime_type": "image/webp",
                    },
                ]
            )
        if tool == "uia_tree":
            return sse_reply(
                [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": True,
                                "window": {
                                    "hwnd": 7,
                                    "pid": 88,
                                    "process": "Blender.exe",
                                    "title": "Blender",
                                },
                                "elements": [
                                    {
                                        "id": "e1",
                                        "role": "Window",
                                        "name": "Blender",
                                        "rect": [0, 0, 640, 360],
                                    },
                                    {
                                        "id": "e2",
                                        "role": "Button",
                                        "name": "Render",
                                        "rect": [10, 10, 60, 20],
                                        "patterns": ["Invoke"],
                                    },
                                ],
                                "truncated": False,
                                "elapsed_ms": 5,
                            }
                        ),
                    }
                ]
            )
        if tool == "lease_manage":
            return sse_reply([{"type": "text", "text": json.dumps({"ok": True, "released": True})}])
        if tool == "mouse_click":
            if aborted["flag"]:
                return sse_reply(
                    [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": False,
                                    "code": CORNER_FAILSAFE_CODE,
                                    "message": "mouse entered top-left corner",
                                }
                            ),
                        }
                    ]
                )
            return sse_reply(
                [
                    {
                        "type": "text",
                        "text": json.dumps({"ok": True, "clicked": [args["x"], args["y"]]}),
                    }
                ]
            )
        if tool == "uia_act":
            return sse_reply(
                [{"type": "text", "text": json.dumps({"ok": True, "clicked": [40, 20]})}]
            )
        return sse_reply([{"type": "text", "text": json.dumps({"ok": True})}])

    tok = "test-bearer-value"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    helper = RemoteWinBridgeHelper(
        "http://127.0.0.1:18792",
        bearer_token=tok,
        holder="meiva-agent-7",
        client=client,
    )

    assert helper.ping()["pong"] is True
    assert helper.screen_size()["w"] == 640.0

    shot_req = tmp_path / "shot.png"
    shot_res = helper.screencapture_region(0, 0, 640, 360, str(shot_req))
    saved = Path(str(shot_res["path"]))
    assert saved.suffix == ".webp"
    assert saved.read_bytes() == webp
    assert shot_res["capture_rect"] == [0, 0, 640, 360]
    assert shot_res["scale_x"] == 1.0

    tree = helper.ax_tree("Blender.exe")
    assert tree["app"] == "Blender.exe"
    act = helper.ax_act("Blender.exe", 2, "press")
    assert act["completed"] is True
    assert any(t == "lease_manage" and a["action"] == "acquire" for t, a in calls)

    aborted["flag"] = True
    with pytest.raises(CornerFailsafeError, match=CORNER_FAILSAFE_CODE):
        helper.click(100, 100)


class _FakeWinHelper(FakeDesktopHelper):
    def __init__(self) -> None:
        super().__init__()
        self.fail_corner = False

    def frontmost_app(self) -> FrontmostApp:
        return {"app": "CC4.exe"}

    def screen_size(self) -> ScreenSize:
        return {"x": 0.0, "y": 0.0, "w": 320.0, "h": 180.0, "scale": 1.0}

    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str) -> dict[str, object]:
        del x, y, w, h
        dst = Path(path).with_suffix(".webp")
        _ = dst.write_bytes(_make_webp_bytes(320, 180))
        return {"path": str(dst), "bytes": dst.stat().st_size}

    def click(
        self,
        x: float,
        y: float,
        *,
        double: bool = False,
        button: str = "left",
        click_count: int | None = None,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> ClickResult:
        if self.fail_corner:
            raise CornerFailsafeError("mouse at (0,0)")
        return super().click(
            x,
            y,
            double=double,
            button=button,
            click_count=click_count,
            modifiers=modifiers,
            duration_ms=duration_ms,
        )


def _setup_headless_daemon(
    database: Database,
    tmp_path: Path,
    win_helper: _FakeWinHelper,
    cluster_calls: list[tuple[str, str, Mapping[str, object]]],
) -> mcp_daemon.ComputerMcpDaemon:
    _ = (tmp_path / "remote_hosts.toml").write_text(
        '[hosts.zzyasus]\nos = "windows"\nlocal_port = 18792\n', encoding="utf-8"
    )
    with database.write_transaction() as conn, conn.cursor() as cur:
        _ = cur.execute(
            "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET gateway_url = EXCLUDED.gateway_url",
            ("macmini", ["agent-runner"], "http://10.20.30.41:8765"),
        )

    def fake_cluster_caller(
        ops_url: str,
        headers: Mapping[str, str],
        tool: str,
        args: Mapping[str, object],
        agent_id: int | None,
        stamp: str,
        home: Path,
    ) -> dict[str, object]:
        del headers, agent_id, stamp
        cluster_calls.append((ops_url, tool, args))
        local_png = home / "logs" / "computer" / "snapshots" / "cluster-snap.png"
        local_png.parent.mkdir(parents=True, exist_ok=True)
        _ = local_png.write_bytes(_make_png_bytes(200, 100))
        return {"path": str(local_png), "source": "screen"}

    return mcp_daemon.ComputerMcpDaemon(
        computer_use_config(),
        database,
        str(tmp_path / "c.sock"),
        helper=None,
        clock=system_clock(),
        home=tmp_path,
        local_machine="ubuntu",
        remote_helper_factory=lambda _cfg, _aid: win_helper,
        cluster_tool_caller=fake_cluster_caller,
    )


async def test_daemon_blocks_ssrf_and_guides_headless_local(
    database: Database, tmp_path: Path
) -> None:
    daemon = _setup_headless_daemon(database, tmp_path, _FakeWinHelper(), [])
    local_err = await daemon._dispatch(
        {"id": 1, "method": "call_tool", "tool": "snapshot", "args": {}, "agent_id": 901}
    )
    assert local_err["ok"] is False
    assert "meiva computer list" in str(local_err.get("error", ""))

    ssrf_err = await daemon._dispatch(
        {
            "id": 2,
            "method": "call_tool",
            "tool": "snapshot",
            "args": {"host": "10.20.30.40"},
            "agent_id": 901,
        }
    )
    assert ssrf_err["ok"] is False
    ssrf_msg = str(ssrf_err.get("error", ""))
    assert "SSRF" in ssrf_msg or "forbidden" in ssrf_msg


async def test_daemon_routes_ssh_with_audit_and_failsafe_release(
    database: Database, tmp_path: Path
) -> None:
    audit_log = AuditLog(database)
    audit_log.clear()
    win_helper = _FakeWinHelper()
    daemon = _setup_headless_daemon(database, tmp_path, win_helper, [])

    ssh_snap = await daemon._dispatch(
        {
            "id": 3,
            "method": "call_tool",
            "tool": "snapshot",
            "args": {"host": "zzyasus"},
            "agent_id": 901,
        }
    )
    assert ssh_snap["ok"] is True
    snap_res = ssh_snap["result"]
    assert is_object_dict(snap_res)
    snap_data = json.loads(text_block(snap_res))
    assert Path(str(snap_data["path"])).is_file()
    assert str(snap_data["path"]).endswith(".webp")
    assert snap_data["pixels"] == {"width": 320, "height": 180}

    win_helper.fail_corner = True
    abort_resp = await daemon._dispatch(
        {
            "id": 4,
            "method": "call_tool",
            "tool": "click",
            "args": {"host": "ssh://zzyasus", "x": 50, "y": 50},
            "agent_id": 901,
        }
    )
    assert abort_resp["ok"] is False
    assert CORNER_FAILSAFE_CODE in str(abort_resp.get("error", ""))
    assert daemon._state_for("ssh://zzyasus").screen.holder is None
    assert "ssh://zzyasus" in [row["payload"].get("target_host") for row in audit_log]


async def test_daemon_routes_cluster_with_audit(database: Database, tmp_path: Path) -> None:
    audit_log = AuditLog(database)
    audit_log.clear()
    cluster_calls: list[tuple[str, str, Mapping[str, object]]] = []
    daemon = _setup_headless_daemon(database, tmp_path, _FakeWinHelper(), cluster_calls)

    cluster_resp = await daemon._dispatch(
        {
            "id": 5,
            "method": "call_tool",
            "tool": "snapshot",
            "args": {"host": "meiva://macmini"},
            "agent_id": 902,
        }
    )
    assert cluster_resp["ok"] is True
    assert cluster_calls[0][0] == "http://10.20.30.41:8765"
    assert cluster_calls[0][1] == "snapshot"
    assert "meiva://macmini" in [row["payload"].get("target_host") for row in audit_log]


def test_remote_snapshot_rejects_traversal_ext_and_oversized_images(tmp_path: Path) -> None:
    png_b64 = base64.b64encode(_make_png_bytes(16, 16)).decode("ascii")

    def make_client(result_obj: dict[str, object]) -> httpx.Client:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": result_obj})

        return httpx.Client(transport=httpx.MockTransport(handler))

    for bad_ext in ("/../../evil", ".exe", ".png/../../escape", "png"):
        with pytest.raises(PermissionsHelperError, match="disallowed screenshot extension"):
            _ = call_cluster_computer_tool(
                "http://10.20.30.41:8765",
                headers={},
                tool="snapshot",
                args={},
                agent_id=7,
                stamp="20261122-000000",
                home=tmp_path,
                client=make_client({"_remote_image_b64": png_b64, "_remote_image_ext": bad_ext}),
            )

    oversized_b64 = "A" * (33 * 1024 * 1024)
    with pytest.raises(PermissionsHelperError, match="byte limit"):
        _ = call_cluster_computer_tool(
            "http://10.20.30.41:8765",
            headers={},
            tool="snapshot",
            args={},
            agent_id=7,
            stamp="20261122-000000",
            home=tmp_path,
            client=make_client({"_remote_image_b64": oversized_b64, "_remote_image_ext": ".png"}),
        )

    rpc_oversized: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "image", "data": oversized_b64, "mime_type": "image/png"}],
            "isError": False,
        },
    }
    parsed = extract_mcp_tool_payload(rpc_oversized)
    assert "byte limit" in (parsed.error_message or "")


def _make_bundle_and_lease_client(
    calls: list[tuple[str, dict[str, object]]],
    remote_holder: dict[str, str | None],
) -> httpx.Client:
    def _lease_reply(args: dict[str, object]) -> dict[str, object]:
        act = args["action"]
        if act == "status":
            holder = remote_holder["holder"]
            return {"ok": True, "lease": {"holder": holder} if holder else None}
        if act == "acquire":
            cur = remote_holder["holder"]
            if cur is not None and cur != args["holder"] and not args.get("force"):
                return {"ok": False, "code": "DESKTOP_LEASE_HELD", "message": f"held by {cur}"}
            remote_holder["holder"] = str(args["holder"])
            return {"ok": True, "acquired": True}
        if act == "release":
            was_held = remote_holder["holder"] is not None
            remote_holder["holder"] = None
            return {"ok": True, "released": was_held}
        return {"ok": True}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tool = str(body["params"]["name"])
        args = dict(body["params"]["arguments"])
        calls.append((tool, args))
        win = {
            "hwnd": 4242,
            "pid": 777,
            "process": "CharacterCreator.exe",
            "title": "Untitled Project",
        }
        if tool == "list_windows":
            payload: dict[str, object] = {
                "ok": True,
                "windows": [{**win, "rect": [0, 0, 1920, 1080], "is_minimized": False}],
            }
        elif tool == "focus_window":
            payload = {"ok": True, "is_foreground": True}
        elif tool == "uia_tree":
            payload = {
                "ok": True,
                "window": win,
                "elements": [],
                "truncated": False,
                "elapsed_ms": 2,
            }
        elif tool == "lease_manage":
            payload = _lease_reply(args)
        else:
            payload = {"ok": True}
        rpc = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        }
        return httpx.Response(200, json=rpc)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_win_bridge_bundle_id_resolution_and_lease_handoff() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    remote_holder: dict[str, str | None] = {"holder": None}
    helper = RemoteWinBridgeHelper(
        "http://127.0.0.1:18792",
        holder="meiva-agent-1",
        lease_seconds=600,
        timers=ManualTimers(),
        client=_make_bundle_and_lease_client(calls, remote_holder),
    )

    focused = helper.focus_app({"bundle_id": "CharacterCreator.exe"})
    assert focused == {"focused": True, "pid": 777}
    by_tool = dict(calls)
    assert (by_tool["focus_window"]["hwnd"], by_tool["focus_window"]["title_contains"]) == (
        4242,
        "",
    )
    assert by_tool["lease_manage"]["ttl_seconds"] == 600

    _ = helper.ax_tree(target={"bundle_id": "CharacterCreator.exe"})
    uia_args = dict(calls[-1][1])
    assert (uia_args["hwnd"], uia_args["title_contains"]) == (4242, "")

    _ = helper.acquire_remote_lease(ttl_seconds=1200)
    assert calls[-1] == (
        "lease_manage",
        {
            "action": "acquire",
            "holder": "meiva-agent-1",
            "purpose": "meiva computer_use session",
            "ttl_seconds": 1200,
        },
    )

    helper.set_holder("meiva-agent-2")
    _ = helper.focus_app({"bundle_id": "CharacterCreator.exe"})
    assert remote_holder["holder"] == "meiva-agent-2"
    lease_calls = [a for t, a in calls if t == "lease_manage"]
    assert lease_calls[-1].get("force") is True

    remote_holder["holder"] = "external-operator"
    helper.set_holder("meiva-agent-3")
    with pytest.raises(PermissionsHelperError, match="DESKTOP_LEASE_HELD"):
        _ = helper.focus_app({"bundle_id": "CharacterCreator.exe"})


async def test_router_rebuilds_on_tunnel_change_and_daemon_runs_ssh_off_loop_and_force_releases(
    database: Database, tmp_path: Path
) -> None:
    _ = (tmp_path / "remote_hosts.toml").write_text(
        '[hosts.zzyasus]\nos = "windows"\n', encoding="utf-8"
    )
    ports = iter(["http://127.0.0.1:21001", "http://127.0.0.1:21002"])

    def fake_endpoint(_cfg: RemoteHostConfig) -> str:
        return next(ports)

    router: RemoteTransportRouter[RemoteWinBridgeHelper] = RemoteTransportRouter(
        db=database,
        clock=system_clock(),
        home=tmp_path,
        lease_s=600.0,
        tunnel_endpoint_resolver=fake_endpoint,
    )
    h1 = router.ssh_helper_for("zzyasus", 1)
    h2 = router.ssh_helper_for("zzyasus", 1)
    assert isinstance(h1, RemoteWinBridgeHelper) and isinstance(h2, RemoteWinBridgeHelper)
    assert h1.base_url == "http://127.0.0.1:21001"
    assert h2.base_url == "http://127.0.0.1:21002"
    assert h1 is not h2

    main_tid = threading.get_ident()
    worker_tids: list[int] = []
    release_calls: list[bool] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        worker_tids.append(threading.get_ident())
        body = json.loads(request.content)
        tool = str(body["params"]["name"])
        args = dict(body["params"]["arguments"])
        if tool == "lease_manage" and args.get("action") == "release":
            release_calls.append(bool(args.get("force")))
            payload: dict[str, object] = {"ok": True, "released": True}
        elif tool == "status":
            payload = {
                "ok": True,
                "screen": {
                    "monitors": [
                        {
                            "index": 1,
                            "role": "primary",
                            "left": 0,
                            "top": 0,
                            "width": 320,
                            "height": 180,
                        }
                    ]
                },
                "foreground_window": {"process": "CC4.exe", "title": "CC4"},
                "session": {"desktop_interactive": True},
            }
        else:
            payload = {"ok": True}
        rpc = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        }
        return httpx.Response(200, json=rpc)

    http_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    daemon = mcp_daemon.ComputerMcpDaemon(
        computer_use_config(),
        database,
        str(tmp_path / "c2.sock"),
        helper=None,
        clock=system_clock(),
        home=tmp_path,
        local_machine="ubuntu",
        remote_helper_factory=lambda _cfg, aid: RemoteWinBridgeHelper(
            "http://127.0.0.1:18792",
            holder=f"meiva-agent-{aid or 0}",
            client=http_client,
        ),
    )

    rel = await daemon._dispatch(
        {
            "id": 10,
            "method": "call_tool",
            "tool": "release_control",
            "args": {"host": "ssh://zzyasus", "force": True},
            "agent_id": None,
        }
    )
    assert rel["ok"] is True
    assert release_calls == [True]
    rel_res = rel["result"]
    assert is_object_dict(rel_res)
    rel_data = json.loads(text_block(rel_res))
    assert rel_data["released"] is None
    assert rel_data["remote_released"] is True

    win_resp = await daemon._dispatch(
        {
            "id": 11,
            "method": "call_tool",
            "tool": "list_apps",
            "args": {"host": "ssh://zzyasus"},
            "agent_id": 55,
        }
    )
    assert win_resp["ok"] is True
    assert worker_tids and all(tid != main_tid for tid in worker_tids)
