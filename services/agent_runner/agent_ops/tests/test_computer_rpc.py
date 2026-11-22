from __future__ import annotations

import base64
import io
import json
import socket
import threading
from pathlib import Path

from PIL import Image

from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import computer_mcp_socket
from services.agent_runner.agent_ops.computer_rpc import computer_rpc_response


def _tiny_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_computer_rpc_status_and_snapshot_transfer(short_tmp: Path) -> None:
    peer_home = short_tmp / "peer"
    sock_path = computer_mcp_socket(peer_home)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    snap_dir = peer_home / "logs" / "computer" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    peer_png = snap_dir / "agent-7-test.png"
    png_bytes = _tiny_png_bytes()
    peer_png.write_bytes(png_bytes)

    seen_args: list[dict[str, object]] = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(4)

    def serve_two() -> None:
        for _ in range(2):
            conn, _ = listener.accept()
            with conn:
                req = json.loads(conn.makefile("rb").readline())
                if req["method"] == "status":
                    reply: dict[str, object] = {
                        "id": req["id"],
                        "ok": True,
                        "result": {"local_available": True, "holder": 7},
                    }
                else:
                    seen_args.append(dict(req["args"]))
                    reply = {
                        "id": req["id"],
                        "ok": True,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps({"path": str(peer_png)})}
                            ],
                            "isError": False,
                        },
                    }
                conn.sendall((json.dumps(reply) + "\n").encode())

    t = threading.Thread(target=serve_two, daemon=True)
    t.start()
    try:
        st_code, st_body, _ = computer_rpc_response(
            json.dumps({"action": "status"}).encode(), home=peer_home, platform_system="darwin"
        )
        assert st_code == 200
        assert json.loads(st_body) == {"ok": True, "available": True, "os": "macos", "holder": 7}

        call_code, call_body, _ = computer_rpc_response(
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "snapshot",
                    "args": {"host": "meiva://macmini"},
                    "agent_id": 42,
                }
            ).encode(),
            home=peer_home,
            platform_system="darwin",
        )
    finally:
        t.join(timeout=5)
        listener.close()

    assert call_code == 200
    assert seen_args == [{}]
    parsed = json.loads(call_body)
    assert parsed["ok"] is True
    result = parsed["result"]
    assert result["_remote_image_ext"] == ".png"
    assert base64.b64decode(result["_remote_image_b64"]) == png_bytes
