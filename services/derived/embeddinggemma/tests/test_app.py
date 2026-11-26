from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.derived.embeddinggemma.app import create_app
from services.derived.embeddinggemma.config import ServerConfig
from services.derived.embeddinggemma.contract import DIM
from services.derived.embeddinggemma.engine import OpenedEngine
from services.derived.embeddinggemma.tests.fakes import (
    MODEL,
    REVISION,
    FakeEngine,
    opener,
    server_config,
)


def _client(tmp_path: Path, engine: FakeEngine, **config: object) -> TestClient:
    return TestClient(create_app(server_config(tmp_path, **config), open_engine=opener(engine)))


def _body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {"model": MODEL, "input": ["alpha", "bb"], "dimensions": DIM}
    body.update(changes)
    return body


def test_embeddings_are_raw_input_ordered_and_echo_the_model(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(tmp_path, engine) as client:
        response = client.post("/v1/embeddings", json=_body())
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == MODEL
    assert [row["index"] for row in body["data"]] == [0, 1]
    assert [row["embedding"][0] for row in body["data"]] == [5.0, 2.0]
    assert engine.calls == [(["alpha", "bb"], True, True, DIM)]


def test_a_single_string_input_and_the_option_flags_pass_through(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(tmp_path, engine) as client:
        response = client.post(
            "/v1/embeddings",
            json=_body(input="solo", normalize=False, insert_special_tokens=False),
        )
    assert response.status_code == 200
    assert engine.calls == [(["solo"], False, False, DIM)]


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"model": "/legacy/model.litertlm,gpu"}, "model mismatch"),
        ({"encoding_format": "base64"}, "encoding_format"),
        ({"dimensions": 256}, "dimensions"),
        ({"input": []}, "non-empty"),
        ({"input": ["ok", "   "]}, "non-empty"),
    ],
)
def test_requests_outside_the_contract_are_rejected(
    tmp_path: Path, changes: dict[str, object], needle: str
) -> None:
    engine = FakeEngine()
    with _client(tmp_path, engine) as client:
        response = client.post("/v1/embeddings", json=_body(**changes))
    assert response.status_code == 400
    assert needle in response.json()["detail"]
    assert engine.calls == []


def test_an_overflow_is_an_error_never_a_truncation(tmp_path: Path) -> None:
    engine = FakeEngine(overflow_over=10)
    with _client(tmp_path, engine) as client:
        response = client.post("/v1/embeddings", json=_body(input=["x" * 11]))
    assert response.status_code == 400
    assert "rejected input" in response.json()["detail"]


def test_healthz_reports_the_pinned_artifact_device_and_overflow_policy(tmp_path: Path) -> None:
    with _client(tmp_path, FakeEngine(), device="gpu") as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model"] == MODEL
    assert body["revision"] == REVISION
    assert body["device"] == "gpu"
    assert body["input_overflow_strategy"] == "ERROR"
    assert body["max_input_length"] == 8192
    assert body["signature_selection"] == [1]
    assert "model_path" not in body


def test_leaving_the_app_closes_the_engine_once(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(tmp_path, engine) as client:
        client.get("/healthz")
        assert engine.closed == 0
    assert engine.closed == 1


def test_a_startup_failure_prevents_serving(tmp_path: Path) -> None:
    def broken(_config: ServerConfig) -> OpenedEngine:
        raise RuntimeError("model SHA256 mismatch")

    app = create_app(server_config(tmp_path), open_engine=broken)
    with pytest.raises(RuntimeError, match="SHA256 mismatch"), TestClient(app):
        pass


def test_a_configured_key_guards_embeddings_but_not_health(tmp_path: Path) -> None:
    app = create_app(
        server_config(tmp_path), open_engine=opener(FakeEngine()), api_key="local-secret"
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/v1/embeddings", json=_body()).status_code == 401
        wrong = client.post(
            "/v1/embeddings", json=_body(), headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401
        right = client.post(
            "/v1/embeddings", json=_body(), headers={"Authorization": "Bearer local-secret"}
        )
        assert right.status_code == 200


def test_a_required_key_missing_from_the_environment_refuses_to_build(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="MEIVA_EMBEDDINGGEMMA_API_KEY"):
        create_app(server_config(tmp_path, require_api_key=True), open_engine=opener(FakeEngine()))
