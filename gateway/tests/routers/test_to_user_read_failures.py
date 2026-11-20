# meiva:note
# The panel read as the browser sees it: the route names each refusal on the wire
# (`code`), so the panel picks its words from the server's class instead of the
# status, and only an unreachable machine says the same request may pass.
# meiva:link gateway/agents/docs/to-user-read-failures.md

import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from base.db import create_agent
from base.paths import workspace_path
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.runner_ops.workspace_ops import (
    register_runner,
    remote_workspace_ops_server,
)
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable


# meiva:note
# The route asks the cluster client for the read, so the test provides that one
# collaborator and leaves the route itself untouched.
class _RefusingRpc:
    def __init__(self, outcome: Exception) -> None:
        self.outcome = outcome

    async def dispatch_to_machine(
        self,
        db: object,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        *,
        timeout_s: float | None = None,
        retries: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        raise self.outcome


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    """A gateway app whose pool runs on the test data plane the app opens at boot."""
    with gateway_test_client() as value:
        yield value


def _home_machine_agent(conn: psycopg.Connection, machine: str) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling')",
        (agent, machine),
    )
    conn.commit()
    return agent


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("too_large", 413, "to_user_document_too_large"),
        ("gone", 404, "to_user_reference_gone"),
        ("unreadable", 422, "to_user_document_unreadable"),
    ],
)
def test_a_refused_read_names_its_class_and_is_never_retryable(
    client: GatewayTestClient, db_conn: psycopg.Connection, reason: str, status: int, code: str
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    client.app.state.rpc = _RefusingRpc(
        ClusterOpFailed({"error": "ValueError: refused", "reason": reason})
    )
    response = client.get(f"/api/agents/{agent}/to-user?file=reports%2Frunbook.md")
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["retryable"] is False


def test_an_unreachable_machine_stays_the_retryable_answer(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    client.app.state.rpc = _RefusingRpc(ClusterOpUnreachable("dial failed"))
    response = client.get(f"/api/agents/{agent}/to-user")
    assert response.status_code == 503
    assert response.json()["retryable"] is True


def test_a_failure_without_a_reason_is_the_unreadable_class(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    client.app.state.rpc = _RefusingRpc(ClusterOpFailed({"error": "RuntimeError: unexpected"}))
    response = client.get(f"/api/agents/{agent}/to-user")
    assert response.status_code == 422
    assert response.json()["code"] == "to_user_document_unreadable"


def test_get_workspace_file_text_success(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "subdir" / "test.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_text("hello world from workspace\nline 2", encoding="utf-8")

    response = client.get(f"/api/agents/{agent}/files/content?path=subdir/test.txt")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "test.txt"
    assert body["extension"] == ".txt"
    assert body["content"] == "hello world from workspace\nline 2"
    assert body["size"] == len(b"hello world from workspace\nline 2")
    assert body["binary"] is False
    assert "modified_at" in body


def test_get_workspace_file_with_line_hash_and_absolute_path(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "code.py"
    _ = target.write_text("print('hi')", encoding="utf-8")

    abs_path = str(target.resolve())
    response = client.get(f"/api/agents/{agent}/files/content?path={abs_path}%23L1-L5")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "code.py"
    assert body["extension"] == ".py"
    assert body["content"] == "print('hi')"


def test_get_workspace_file_image_returns_base64(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "icon.png"
    png_bytes = b"\x89PNG\r\n\x1a\nfakeimagebytes"
    _ = target.write_bytes(png_bytes)

    response = client.get(f"/api/agents/{agent}/files/content?path=icon.png")
    assert response.status_code == 200
    body = response.json()
    assert body["binary"] is True
    assert body["extension"] == ".png"
    assert body["content"] == ""

    raw_resp = client.get(f"/api/agents/{agent}/files/raw?path=icon.png")
    assert raw_resp.status_code == 200
    assert raw_resp.content == png_bytes
    assert raw_resp.headers["content-type"] == "image/png"


def test_get_workspace_file_raw_video_range_request(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "clip.mp4"
    data = b"0123456789abcdefghij"
    _ = target.write_bytes(data)

    range_resp = client.get(
        f"/api/agents/{agent}/files/raw?path=clip.mp4",
        headers={"Range": "bytes=0-4"},
    )
    assert range_resp.status_code == 206
    assert range_resp.content == b"01234"
    assert range_resp.headers["content-range"] == "bytes 0-4/20"
    assert range_resp.headers["accept-ranges"] == "bytes"

    whole = client.get(f"/api/agents/{agent}/files/raw?path=clip.mp4")
    assert whole.status_code == 200
    assert whole.headers["accept-ranges"] == "bytes"


# meiva:note
# The viewer shows a PDF in an <iframe>/<embed> and plays audio in <audio>, so both render inline
# under the passive policy rather than downloading.
@pytest.mark.parametrize(
    ("name", "media_type"), [("report.pdf", "application/pdf"), ("voice.mp3", "audio/mpeg")]
)
def test_pdf_and_audio_render_inline_under_the_passive_policy(
    client: GatewayTestClient, db_conn: psycopg.Connection, name: str, media_type: str
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    _ = (ws / name).write_bytes(b"payload")

    response = client.get(f"/api/agents/{agent}/files/raw?path={name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["content-security-policy"].startswith("sandbox;")


def test_get_workspace_file_in_custom_cwd(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    _ = (ws / "ws_file.txt").write_text("in workspace", encoding="utf-8")

    custom_cwd = tmp_path / "custom_project"
    custom_cwd.mkdir(parents=True, exist_ok=True)
    _ = (custom_cwd / "cwd_file.txt").write_text("in custom cwd", encoding="utf-8")

    import json

    ckpt = json.dumps({"channel_values": {"meiva_code__cwd": str(custom_cwd)}})
    _ = db_conn.execute(
        """
        INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)
        VALUES (%s, '', '1', 'json', %s::jsonb, '{}'::jsonb)
        """,
        (str(agent), ckpt),
    )
    db_conn.commit()

    resp_cwd = client.get(f"/api/agents/{agent}/files/content?path=cwd_file.txt")
    assert resp_cwd.status_code == 200
    assert resp_cwd.json()["content"] == "in custom cwd"

    resp_ws = client.get(f"/api/agents/{agent}/files/content?path=ws_file.txt")
    assert resp_ws.status_code == 200
    assert resp_ws.json()["content"] == "in workspace"


def test_get_workspace_file_traversal_forbidden(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    response = client.get(f"/api/agents/{agent}/files/content?path=../../etc/passwd")
    assert response.status_code == 403


def test_get_workspace_file_nonexistent_not_found(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    response = client.get(f"/api/agents/{agent}/files/content?path=nonexistent.txt")
    assert response.status_code == 404


def test_get_workspace_file_too_large_413(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _home_machine_agent(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "big.bin"
    _ = target.write_bytes(b"a" * (1024 * 1024 + 10))

    response = client.get(f"/api/agents/{agent}/files/content?path=big.bin")
    assert response.status_code == 413


def test_get_workspace_file_unknown_agent_404(client: GatewayTestClient) -> None:
    response = client.get("/api/agents/9999999/files/content?path=file.txt")
    assert response.status_code == 404


def test_cross_agent_workspace_file_routes_same_machine_and_remote_machine(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    viewer_agent = _home_machine_agent(db_conn, "macmini")
    same_host_owner = _home_machine_agent(db_conn, "macmini")
    remote_owner = _home_machine_agent(db_conn, "company-mini")

    ws_viewer = Path(workspace_path(client.app.state.gateway_process.home, viewer_agent))
    ws_viewer.mkdir(parents=True, exist_ok=True)

    ws_same = Path(workspace_path(client.app.state.gateway_process.home, same_host_owner))
    (ws_same / "deliver").mkdir(parents=True, exist_ok=True)
    _ = (ws_same / "deliver" / "report.md").write_text("# Local peer report\n", encoding="utf-8")
    png_bytes = b"\x89PNG\r\n\x1a\ncross-agent-image"
    _ = (ws_same / "deliver" / "chart.png").write_bytes(png_bytes)

    same_content = client.get(
        f"/api/agents/{viewer_agent}/files/content"
        f"?path=/Users/zhiyuan-output/.meiva/workspaces/{same_host_owner}/deliver/report.md"
    )
    assert same_content.status_code == 200
    assert same_content.json()["content"] == "# Local peer report\n"

    same_raw = client.get(
        f"/api/agents/{viewer_agent}/files/raw"
        f"?path=/Users/zhiyuan-output/.meiva/workspaces/{same_host_owner}/deliver/chart.png"
    )
    assert same_raw.status_code == 200
    assert same_raw.content == png_bytes

    remote_home = tmp_path / "company-mini-home"
    ws_remote = workspace_path(remote_home, remote_owner)
    (ws_remote / "deliver").mkdir(parents=True, exist_ok=True)
    _ = (ws_remote / "deliver" / "remote.md").write_text("# Remote peer report\n", encoding="utf-8")
    _ = (ws_remote / "deliver" / "user-positions-before-after.png").write_bytes(png_bytes)

    with remote_workspace_ops_server(remote_home, remote_owner) as remote_ops_url:
        register_runner(db_conn, "company-mini", remote_ops_url)

        remote_content = client.get(
            f"/api/agents/{viewer_agent}/files/content"
            f"?path=/Users/zhiyuan-output/.meiva/workspaces/{remote_owner}/deliver/remote.md"
        )
        assert remote_content.status_code == 200
        assert remote_content.json()["content"] == "# Remote peer report\n"

        remote_raw = client.get(
            f"/api/agents/{viewer_agent}/files/raw"
            f"?path=file:///Users/zhiyuan-output/.meiva/workspaces/{remote_owner}/deliver/user-positions-before-after.png"
        )
        assert remote_raw.status_code == 200
        assert remote_raw.content == png_bytes
        assert remote_raw.headers["content-type"] == "image/png"


def test_cross_agent_traversal_and_system_paths_return_403(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent_a = _home_machine_agent(db_conn, "macmini")
    agent_b = _home_machine_agent(db_conn, "macmini")
    Path(workspace_path(client.app.state.gateway_process.home, agent_a)).mkdir(
        parents=True, exist_ok=True
    )
    ws_b = Path(workspace_path(client.app.state.gateway_process.home, agent_b))
    ws_b.mkdir(parents=True, exist_ok=True)
    _ = (ws_b / "secret.txt").write_text("secret", encoding="utf-8")

    for forbidden in (
        f"/Users/other/.meiva/workspaces/{agent_a}/../{agent_b}/secret.txt",
        f"/Users/other/.meiva/workspaces/{agent_b}/deliver/../../etc/passwd",
        "/etc/passwd",
        "/Users/other/secret.txt",
    ):
        resp_content = client.get(f"/api/agents/{agent_a}/files/content?path={forbidden}")
        assert resp_content.status_code == 403
        resp_raw = client.get(f"/api/agents/{agent_a}/files/raw?path={forbidden}")
        assert resp_raw.status_code == 403


def test_eval_isolated_caller_denied_on_cross_workspace_file_read(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    viewer_agent = _home_machine_agent(db_conn, "macmini")
    owner_agent = _home_machine_agent(db_conn, "macmini")
    isolated_caller = _home_machine_agent(db_conn, "macmini")
    _ = db_conn.execute(
        "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
        (json.dumps({"eval_isolation": True}), isolated_caller),
    )
    db_conn.commit()
    ws_owner = Path(workspace_path(client.app.state.gateway_process.home, owner_agent))
    ws_owner.mkdir(parents=True, exist_ok=True)
    _ = (ws_owner / "report.md").write_text("hello", encoding="utf-8")

    resp = client.get(
        f"/api/agents/{viewer_agent}/files/content"
        f"?path=/Users/other/.meiva/workspaces/{owner_agent}/report.md"
        f"&caller=agent:{isolated_caller}"
    )
    assert resp.status_code == 403
    assert "eval-isolated" in resp.json()["detail"]
