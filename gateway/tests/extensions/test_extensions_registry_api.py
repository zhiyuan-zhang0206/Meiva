import base64
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.packages.extensions import registry
from base.packages.extensions.install_registry import tree_hash
from gateway.extensions.registry import MAX_ARCHIVE_B64_CHARS
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client


def _tree(root: Path, body: str = "# demo\n") -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(body, encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "REFERENCE.md").write_text("ref\n", encoding="utf-8")
    return root


def _body(
    root: Path, *, source: str = "local:laptop", source_ref: str | None = None
) -> dict[str, Any]:
    return {
        "source": source,
        "source_ref": source_ref,
        "archive_b64": base64.b64encode(registry.pack_tree(root)).decode("ascii"),
    }


def test_put_registers_the_tree_and_returns_its_hash(
    client: TestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    root = _tree(tmp_path / "demo")
    response = client.put("/api/extensions/skill/demo", json=_body(root, source_ref=None))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "demo",
        "kind": "skill",
        "content_hash": tree_hash(root),
    }
    row = registry.get(db_conn, "demo")
    assert row is not None
    assert (row.kind, row.source, row.trust, row.default_enabled) == (
        "skill",
        "local:laptop",
        "unreviewed",
        True,
    )
    assert row.content_hash == tree_hash(root)
    archive = registry.get_blob(db_conn, tree_hash(root))
    assert archive is not None and archive == registry.pack_tree(root)


def test_replay_is_idempotent(
    client: TestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    root = _tree(tmp_path / "demo")
    first = client.put("/api/extensions/skill/demo", json=_body(root))
    second = client.put("/api/extensions/skill/demo", json=_body(root))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert db_conn.execute("SELECT count(*) FROM extensions").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM extension_blobs").fetchone() == (1,)


def test_changed_content_replaces_the_row_and_resets_trust(
    client: TestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    v1 = _tree(tmp_path / "v1", "one\n")
    v2 = _tree(tmp_path / "v2", "two\n")
    assert client.put("/api/extensions/skill/demo", json=_body(v1)).status_code == 200
    assert client.put("/api/extensions/skill/demo", json=_body(v2)).status_code == 200
    row = registry.get(db_conn, "demo")
    assert row is not None and row.content_hash == tree_hash(v2)


def test_oversized_archive_is_refused_before_decoding(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    body = {
        "source": "local:laptop",
        "source_ref": None,
        "archive_b64": "A" * (MAX_ARCHIVE_B64_CHARS + 4),
    }
    response = client.put("/api/extensions/skill/big", json=body)
    assert response.status_code == 413
    assert registry.get(db_conn, "big") is None


def test_archive_over_the_cap_after_decoding_is_refused(
    client: TestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    root = tmp_path / "big"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"x" * (registry.MAX_BLOB_BYTES + 1))
    response = client.put("/api/extensions/skill/big", json=_body(root))
    assert response.status_code == 413, response.text[:200]
    assert registry.get(db_conn, "big") is None


@pytest.mark.parametrize("archive_b64", ["not base64!!", base64.b64encode(b"not a tar").decode()])
def test_unusable_archive_is_422(
    client: TestClient, db_conn: psycopg.Connection, archive_b64: str
) -> None:
    body = {"source": "local:laptop", "source_ref": None, "archive_b64": archive_b64}
    assert client.put("/api/extensions/skill/bad", json=body).status_code == 422
    assert registry.get(db_conn, "bad") is None


@pytest.mark.parametrize(
    "path",
    ["/api/extensions/npm/demo", "/api/extensions/skill/bad%20name", "/api/extensions/skill/-x"],
)
def test_unknown_kind_or_unsafe_name_is_422(client: TestClient, tmp_path: Path, path: str) -> None:
    root = _tree(tmp_path / "demo")
    assert client.put(path, json=_body(root)).status_code == 422


def test_unauthenticated_request_is_401_and_writes_nothing(
    client: TestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    root = _tree(tmp_path / "demo")
    response = client.put(
        "/api/extensions/skill/demo",
        json=_body(root),
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401
    assert registry.get(db_conn, "demo") is None
