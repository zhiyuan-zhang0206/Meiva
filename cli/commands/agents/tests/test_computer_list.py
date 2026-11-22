from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import computer_mcp_socket
from cli.commands.agents.computer import cmd_computer_list, cmd_computer_release

_SAMPLE_TOML = """
[hosts.zzyasus]
os = "windows"
ssh_host = "zzyasus"
helper_port = 18792
local_port = 18792
token_file = "/secret/path/zzyasus.token"
notes = "Win11 · RTX 2060 · CC4 / Headshot"

[hosts.winlab]
os = "windows"
ssh_host = "winlab"
notes = "On-demand lab box"
"""

_CLUSTER_MACHINES = [
    ("macmini", "http://10.20.30.41:8765", "Home Mac mini"),
    ("ubuntu", "http://10.20.30.42:8765", "Headless Ubuntu"),
    ("company-mini", "http://10.20.30.43:8765", "Office Mac mini"),
]


def _fake_cluster_probe(url: str) -> dict[str, object]:
    if "10.20.30.41" in url:
        return {"ok": True, "available": True, "os": "macos", "holder": 405}
    if "10.20.30.42" in url:
        return {"ok": True, "available": False, "os": "linux", "holder": None}
    return {"ok": False, "available": False, "error": "connection refused"}


def _fake_ssh_probe(url: str) -> dict[str, object] | None:
    if url == "http://127.0.0.1:18792":
        return {"ok": True, "lease_holder": "meiva-agent-7507", "session_aborted": False}
    return None


def test_computer_list_table_hides_secrets_and_self(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = (tmp_path / "remote_hosts.toml").write_text(_SAMPLE_TOML, encoding="utf-8")
    rc = cmd_computer_list(
        home=tmp_path,
        platform_system="linux",
        local_machine="ubuntu",
        cluster_machines=_CLUSTER_MACHINES,
        cluster_status_probe=_fake_cluster_probe,
        ssh_health_probe=_fake_ssh_probe,
        json_output=False,
    )
    assert rc == 0
    table_out = capsys.readouterr().out
    for expected in (
        "TARGET",
        "HELD_BY",
        "meiva://macmini",
        "agent:405",
        "ssh://zzyasus",
        "meiva-agent-7507",
        "ssh://winlab",
        "configured (on-demand ssh)",
    ):
        assert expected in table_out
    for forbidden in ("meiva://ubuntu", "zzyasus.token", "/secret/path"):
        assert forbidden not in table_out


def test_computer_list_json_output_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = (tmp_path / "remote_hosts.toml").write_text(_SAMPLE_TOML, encoding="utf-8")
    rc_json = cmd_computer_list(
        home=tmp_path,
        platform_system="linux",
        local_machine="ubuntu",
        cluster_machines=_CLUSTER_MACHINES,
        cluster_status_probe=_fake_cluster_probe,
        browser_status_probe=lambda url: "10.20.30.41" in url,
        ssh_health_probe=_fake_ssh_probe,
        json_output=True,
    )
    assert rc_json == 0
    raw_json = capsys.readouterr().out
    assert "zzyasus.token" not in raw_json
    rows = json.loads(raw_json)
    by_target = {r["target"]: r for r in rows}
    assert by_target["meiva://macmini"] == {
        "target": "meiva://macmini",
        "os": "macos",
        "status": "online",
        "held_by": "agent:405",
        "notes": "Home Mac mini",
        "browser_status": "online",
    }
    assert by_target["ssh://zzyasus"] == {
        "target": "ssh://zzyasus",
        "os": "windows",
        "status": "online",
        "held_by": "meiva-agent-7507",
        "notes": "Win11 · RTX 2060 · CC4 / Headshot",
        "browser_status": "unprobed (SSH)",
    }
    assert by_target["ssh://winlab"]["held_by"] is None
    assert by_target["meiva://company-mini"]["status"] == "offline"


def test_computer_release_forwards_optional_host(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sock = computer_mcp_socket(short_tmp)
    sock.parent.mkdir(parents=True)
    seen: list[dict[str, object]] = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            seen.append(json.loads(conn.makefile("rb").readline()))
            payload = json.dumps({"released": 7507})
            conn.sendall(
                (
                    json.dumps({"ok": True, "result": {"content": [{"text": payload}]}}) + "\n"
                ).encode()
            )

    t = threading.Thread(target=serve)
    t.start()
    try:
        rc = cmd_computer_release(home=short_tmp, host="ssh://zzyasus")
    finally:
        t.join(timeout=5)
        listener.close()

    assert rc == 0
    assert seen == [
        {
            "id": 1,
            "method": "call_tool",
            "tool": "release_control",
            "args": {"force": True, "host": "ssh://zzyasus"},
        }
    ]
    assert capsys.readouterr().out == "released the screen from agent 7507\n"
