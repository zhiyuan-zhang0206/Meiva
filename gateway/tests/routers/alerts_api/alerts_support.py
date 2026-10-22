from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from base.config import settings
from base.telemetry.alerts.native import load_native_group, stamp_native_sent
from gateway.alerts import router as alerts_router
from gateway.tests.auth.gateway_config import gateway_test_client


def native_sender(callback: Callable[[str], bool]) -> Callable[[int], bool]:
    # meiva:note
    # The policy fixture reports real-send success; acceptance-only tests live with Outbox.

    def send(group_id: int) -> bool:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            source = load_native_group(conn, group_id)
            succeeded = callback(source["text"])
            if succeeded:
                stamp_native_sent(conn, group_id)
            return succeeded

    return send


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
def ingest(
    client: TestClient,
    payload: dict[str, Any],
    token: str = "test-token",  # noqa: S107
) -> Any:
    return client.post(
        "/api/alerts",
        json=payload,
        headers={"X-Alerts-Token": token},
    )


@pytest.fixture
def client() -> Any:
    """TestClient with the gateway app (lifespan: db pool + scheduler)."""

    with gateway_test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def alerts_auth_and_im(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook token set (loopback trust off) + IM fan-out mocked."""

    monkeypatch.setattr(settings.alerts, "webhook_token", SecretStr("test-token"))
    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)

    def _fake_notify(text: str) -> bool:
        return True

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_fake_notify))


def seed_alert_rows(db: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    with db.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO alerts"
                " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
                "  fingerprint, source)"
                " VALUES (%(status)s, %(severity)s, %(alertname)s, '{}'::jsonb, '{}'::jsonb,"
                "         %(starts_at)s, %(ends_at)s, %(fingerprint)s, %(source)s)",
                {
                    **r,
                    "source": r.get("source", "grafana"),
                    "labels": None,
                    "annotations": None,
                },
            )
    db.commit()


def capture_im(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True) -> list[str]:
    sent: list[str] = []

    def _capture(text: str) -> bool:
        sent.append(text)
        return ok

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_capture))
    return sent
