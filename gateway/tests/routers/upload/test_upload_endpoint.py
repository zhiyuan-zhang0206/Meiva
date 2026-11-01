from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from pathlib import Path

import psycopg
import pytest

from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import PROTOCOL, CopyProof, current_unit
from base.agents.upload_delivery.paths import agent_upload_dir, delivered_upload_dir
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.paths import resolve_meiva_home
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
    spawn_via_http,
)
from gateway.tests.support.runner_ops.ops_stub import RunnerOps

pytestmark = pytest.mark.usefixtures("gateway_local_runner")


def _spawn_agent() -> int:
    with gateway_test_client() as client:
        resp = spawn_via_http(client, {})
        assert resp.status_code == 201
        return resp.json()["id"]


# meiva:note
# A gateway whose uploads live under `home` and whose upload limits are `limits`.
def _upload_client(home: Path, **limits: int) -> AbstractContextManager[GatewayTestClient]:
    return gateway_test_client(config=gateway_test_config(upload_staging_root=home, **limits))


def _write_historical_flat_upload(home: Path, agent_id: int, name: str, data: bytes) -> str:
    directory = agent_upload_dir(agent_id, root=home)
    (directory / name).write_bytes(data)
    return f"/api/agents/{agent_id}/uploads/{name}"


def _stage_and_accept_batch(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    files: list[tuple[str, bytes, str]],
    *,
    machine: str,
    runner_url: str,
) -> tuple[str, Path]:
    unit = current_unit(local_machine=machine, home=resolve_meiva_home())
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
        "VALUES (%s,%s,true,%s) ON CONFLICT (machine_name, home) DO UPDATE SET url=EXCLUDED.url",
        (unit.machine, unit.home, runner_url),
    )
    db_conn.commit()
    file_ids: list[str] = []
    for name, data, content_type in files:
        staged = client.post(
            f"/api/keyed/v1/agents/{agent_id}/uploads/staged",
            headers={"Idempotency-Key": uuid.uuid4().hex, "Idempotency-Scope": "principal-v1"},
            files={"file": (name, data, content_type)},
        )
        assert staged.status_code == 201, staged.text
        file_ids.append(staged.json()["file_id"])

    sent = client.post(
        f"/api/keyed/v1/agents/{agent_id}/uploads",
        headers={"Idempotency-Key": uuid.uuid4().hex, "Idempotency-Scope": "principal-v1"},
        json={"file_ids": file_ids, "text": "attached"},
    )
    assert sent.status_code == 202, sent.text
    batch_id = sent.json()["batch_id"]
    pool = client.app.state.db_pool
    request = next(
        item
        for item in source.due(
            pool, local_machine=machine, home=resolve_meiva_home(), claim_seconds=0.0
        )
        if item.manifest.batch_id == batch_id
    )
    upload_dir = delivered_upload_dir(
        agent_id,
        home=resolve_meiva_home(),
        name=client.app.state.gateway_config.upload_dir_name,
        create=True,
    ).resolve()
    batch_dir = upload_dir / request.manifest.directory
    batch_dir.mkdir(parents=True, exist_ok=True)
    for item, (_name, data, _content_type) in zip(request.manifest.objects, files, strict=True):
        (batch_dir / item.name).write_bytes(data)
    proof = CopyProof(
        version=PROTOCOL,
        target=unit,
        manifest_hash=request.manifest.fingerprint(),
        directory=str(batch_dir),
    )
    source.complete(pool, request, proof)
    return batch_id, batch_dir


class TestLegacyUploadRouteRemoved:
    def test_post_legacy_upload_route_is_removed(self, tmp_path: Path) -> None:
        agent_id = _spawn_agent()
        with _upload_client(tmp_path) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                headers={"Idempotency-Key": uuid.uuid4().hex, "Idempotency-Scope": "principal-v1"},
                files=[("files", ("hello.txt", b"Hello", "text/plain"))],
            )
        assert resp.status_code in (404, 405)


class TestServeUpload:
    def test_serves_saved_file(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        del db_conn
        agent_id = _spawn_agent()
        raw = b"\x89PNG\r\n\x1a\npixels"
        url = _write_historical_flat_upload(tmp_path, agent_id, "shot.png", raw)
        with _upload_client(tmp_path) as client:
            resp = client.get(url)
        assert resp.status_code == 200
        assert resp.content == raw

    def test_renderable_upload_served_as_attachment(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        del db_conn
        agent_id = _spawn_agent()
        url = _write_historical_flat_upload(
            tmp_path, agent_id, "page.html", b"<script>alert(1)</script>"
        )
        with _upload_client(tmp_path) as client:
            resp = client.get(url)
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-disposition"].startswith("attachment")

    def test_plain_upload_served_with_nosniff_only(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        del db_conn
        agent_id = _spawn_agent()
        url = _write_historical_flat_upload(tmp_path, agent_id, "shot.png", b"\x89PNG")
        with _upload_client(tmp_path) as client:
            resp = client.get(url)
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in resp.headers

    def test_missing_file_404(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        del db_conn
        agent_id = _spawn_agent()
        with _upload_client(tmp_path) as client:
            resp = client.get(f"/api/agents/{agent_id}/uploads/nope.png")
        assert resp.status_code == 404

    def test_traversal_rejected(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        del db_conn
        agent_id = _spawn_agent()
        with _upload_client(tmp_path) as client:
            resp = client.get(f"/api/agents/{agent_id}/uploads/..%2f..%2fsecret")
        assert resp.status_code in (400, 404)

    def test_serve_unknown_agent_404(self, tmp_path: Path) -> None:
        with _upload_client(tmp_path) as client:
            resp = client.get("/api/agents/99999/uploads/x.png")
        assert resp.status_code == 404


class TestServeDeliveredUpload:
    def test_serves_raster_image_inline_and_svg_or_html_as_attachment(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        machine_identity: MachineIdentityReader,
        gateway_local_runner: RunnerOps,
    ) -> None:
        agent_id = _spawn_agent()
        png_bytes = b"\x89PNG\r\n\x1a\npixels"
        svg_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        with _upload_client(tmp_path, upload_recovery_enabled=False) as client:
            batch_id, _directory = _stage_and_accept_batch(
                client,
                db_conn,
                agent_id,
                [
                    ("shot.png", png_bytes, "image/png"),
                    ("icon.svg", svg_bytes, "image/svg+xml"),
                ],
                machine=machine_identity.name(),
                runner_url=gateway_local_runner.url,
            )
            png_resp = client.get(f"/api/agents/{agent_id}/uploads/{batch_id}/0")
            svg_resp = client.get(f"/api/agents/{agent_id}/uploads/{batch_id}/1")

        assert png_resp.status_code == 200
        assert png_resp.content == png_bytes
        assert png_resp.headers["content-disposition"].startswith("inline;")
        assert png_resp.headers["x-content-type-options"] == "nosniff"
        assert png_resp.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert png_resp.headers["cache-control"] == "private, max-age=31536000, immutable"

        assert svg_resp.status_code == 200
        assert svg_resp.content == svg_bytes
        assert svg_resp.headers["content-disposition"].startswith("attachment;")
        assert svg_resp.headers["x-content-type-options"] == "nosniff"

    def test_modified_or_deleted_workspace_file_returns_404(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        machine_identity: MachineIdentityReader,
        gateway_local_runner: RunnerOps,
    ) -> None:
        agent_id = _spawn_agent()
        with _upload_client(tmp_path, upload_recovery_enabled=False) as client:
            batch_id, directory = _stage_and_accept_batch(
                client,
                db_conn,
                agent_id,
                [("report.txt", b"original", "text/plain")],
                machine=machine_identity.name(),
                runner_url=gateway_local_runner.url,
            )
            target_file = directory / "report.txt"
            target_file.write_bytes(b"modified")
            modified_resp = client.get(f"/api/agents/{agent_id}/uploads/{batch_id}/0")
            target_file.unlink()
            deleted_resp = client.get(f"/api/agents/{agent_id}/uploads/{batch_id}/0")

        assert modified_resp.status_code == 404
        assert deleted_resp.status_code == 404
