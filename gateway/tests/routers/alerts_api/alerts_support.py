from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
)


def alert(
    *,
    status: str = "firing",
    alertname: str = "test-rule",
    severity: str | None = "error",
    starts_at: str = "2026-08-04T10:00:00Z",
    ends_at: str = "",
    summary: str = "test summary",
    fingerprint: str = "abc123",
    notify_im: str | None = None,
) -> dict[str, Any]:
    labels = {"alertname": alertname, "team": "meiva-ops"}
    if severity:
        labels["severity"] = severity
    if notify_im is not None:
        labels["notify_im"] = notify_im
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": summary},
        "startsAt": starts_at,
        "endsAt": ends_at,
        "fingerprint": fingerprint,
        "generatorURL": "http://localhost:3002/alerting/xyz/edit",
    }


def webhook(
    status: str = "firing",
    alerts: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "alerts": alerts or [alert(status=status, **extra)],
    }
    return payload


# meiva:note
# Test token fixture value.
def post_webhook(
    client: TestClient,
    payload: dict[str, Any],
    token: str = "test-token",  # noqa: S107
) -> Any:
    return client.post(
        "/api/alerts",
        json=payload,
        headers={"X-Alerts-Token": token},
    )


# meiva:note
# The gateway app with the webhook token set (loopback trust off).
#
# `cluster_secret` pins the app's cluster secret; `client_options` reach the test client (the
# address a request appears to come from, for example).
@contextmanager
def alerts_client(
    *, cluster_secret: str | None = None, **client_options: Any
) -> Generator[GatewayTestClient]:
    fields = {"webhook_token": SecretStr("test-token")}
    with gateway_test_client(fields=fields, cluster_secret=cluster_secret, **client_options) as c:
        yield c


@pytest.fixture
def client() -> Generator[GatewayTestClient]:
    """TestClient with the gateway app (lifespan: db pool + scheduler)."""
    with alerts_client() as c:
        yield c
