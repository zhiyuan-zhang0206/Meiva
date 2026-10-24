# meiva:note
# POST /api/alerts + GET /api/alerts + GET /api/alerts/stream integration tests
# (Task #1224 — the Alert system, separate from Notice).
#
# Same posture as the old test_ops_alerts_api.py: real SQL on the session DB.
# Locks the contract: Alertmanager-webhook upsert + (fingerprint, starts_at)
# dedup, severity label parsing (critical/warning/error), the fingerprint
# computation when the payload omits it, the unresolved-first list + counts
# (including the badge's unresolved count), the ingest auth split (webhook
# token / loopback), the SSE publish on every
# ingest, and the IM-notify gate (the im_bridge fan-out is mocked — the
# endpoint's side effect is that it POSTs to the daemon, which has its own
# tests).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from base.events.live.bus import EventBus
from gateway.tests.agents.gateway_auth import without_credentials
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.alerts_api.alerts_support import (
    alert,
    alert_frames,
    alerts_client,
    capture_im,
    ingest,
    webhook,
)
from gateway.tests.routers.alerts_api.alerts_support import (
    alerts_im as alerts_im,
)
from gateway.tests.routers.alerts_api.alerts_support import (
    client as client,
)

# meiva:note
# -- ingest ------------------------------------------------------------------


# meiva:note
# A firing webhook inserts one row (unresolved), notifies IM once, stamps
# notified_at.
def test_ingest_firing_inserts_row_and_notifies(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        resp = ingest(client, webhook())
        assert resp.status_code == 200
        assert resp.json() == {"processed": 1, "inserted": 1, "updated": 0, "notified": 1}

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, severity, alertname, fingerprint, notified_at FROM alerts")
        rows = cur.fetchall()
    assert len(rows) == 1
    status, severity, alertname, fp, notified_at = rows[0]
    assert status == "unresolved"
    assert severity == "error"
    assert alertname == "test-rule"
    assert fp == "abc123"
    assert notified_at is not None


# meiva:note
# Re-sends of the same (fingerprint, starts_at) update the row, not
# duplicate it — and a still-firing instance that already notified stays
# silent on IM.
def test_ingest_dedups_same_instance(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook())
        resp = ingest(client, webhook(summary="updated summary"))
        assert resp.json()["inserted"] == 0
        assert resp.json()["updated"] == 1
        assert resp.json()["notified"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(annotations->>'summary') FROM alerts")
        row = cur.fetchone()
        assert row is not None
        n, summary = row
    assert n == 1
    assert summary == "updated summary"


# meiva:note
# Escalation is a new firing transition on the existing instance.
def test_ingest_severity_escalation_updates_instance_and_renotifies(
    db_conn: psycopg.Connection,
) -> None:
    with alerts_client() as client:
        warning = ingest(client, webhook(severity="warning"))
        error = ingest(client, webhook(severity="error"))

    assert warning.json()["notified"] == 1
    assert error.json()["notified"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(severity) FROM alerts")
        row = cur.fetchone()
    assert row == (1, "error")


# meiva:note
# A lower class carries no new urgent information for the owner.
def test_ingest_severity_downgrade_does_not_renotify(
    db_conn: psycopg.Connection,
) -> None:
    with alerts_client() as client:
        error = ingest(client, webhook(severity="error"))
        warning = ingest(client, webhook(severity="warning"))

    assert error.json()["notified"] == 1
    assert warning.json()["notified"] == 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(severity) FROM alerts")
        row = cur.fetchone()
    assert row == (1, "warning")


# meiva:note
# A resolved webhook for a notified firing flips status + sets ends_at
# and notifies the recovery line.
def test_ingest_resolved_flips_row_and_notifies_recovery(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook())
        resp = ingest(
            client,
            webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"),
        )
        assert resp.json()["notified"] == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, ends_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
    assert row[0] == "resolved"
    assert row[1] == datetime(2026, 8, 4, 11, 0, tzinfo=UTC)


# meiva:note
# An instance whose firing IM never landed resolves silently (the user
# never heard the firing; a bare recovery line would be noise).
def test_ingest_resolved_without_prior_notify_is_silent(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (status, severity, alertname, labels, annotations,"
                " starts_at, fingerprint) VALUES ('unresolved', 'error', 'test-rule',"
                " '{}', '{}', '2026-08-04T10:00:00+00:00', 'abc123')"
            )
        db_conn.commit()
        resp = ingest(
            client,
            webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"),
        )
        assert resp.json()["notified"] == 0


# meiva:note
# critical/warning/error parse; an unknown/absent severity normalizes to
# warning (the quietest default).
def test_ingest_severity_parsing(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook(alerts=[alert(severity="critical", fingerprint="f1")]))
        ingest(client, webhook(alerts=[alert(severity="warning", fingerprint="f2")]))
        ingest(client, webhook(alerts=[alert(severity="error", fingerprint="f3")]))
        ingest(client, webhook(alerts=[alert(severity="BOGUS", fingerprint="f8")]))
        ingest(client, webhook(alerts=[alert(severity=None, fingerprint="f9")]))

    with db_conn.cursor() as cur:
        cur.execute("SELECT fingerprint, severity FROM alerts ORDER BY fingerprint")
        rows = cur.fetchall()
    assert dict(rows) == {
        "f1": "critical",
        "f2": "warning",
        "f3": "error",
        "f8": "warning",
        "f9": "warning",
    }


# meiva:note
# User design 2026-08-12: ALL three severities push to IM — there is no
# severity gate anymore.
def test_ingest_every_severity_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    notified = capture_im(monkeypatch)
    with alerts_client() as client:
        ingest(client, webhook(alerts=[alert(severity="critical", fingerprint="c1")]))
        ingest(client, webhook(alerts=[alert(severity="warning", fingerprint="w1")]))
        ingest(client, webhook(alerts=[alert(severity="error", fingerprint="e1")]))
    assert len(notified) == 3
    # meiva:note
    # default template language is zh (user ruling 2026-08-13: IM copy follows
    # user_settings display.language, default zh) — the en path is covered by
    # test_ingest_uses_display_language_setting
    assert (
        # meiva:note
        # emoji-ok: IM alert head format
        "\u26a0\ufe0f \u544a\u8b66 [CRITICAL]" in notified[0]
    )
    assert (
        # meiva:note
        # emoji-ok: IM alert head format
        "\u26a0\ufe0f \u544a\u8b66 [WARNING]" in notified[1]
    )
    assert (
        # meiva:note
        # emoji-ok: IM alert head format
        "\u26a0\ufe0f \u544a\u8b66 [ERROR]" in notified[2]
    )


# meiva:note
# notify_im="false" keeps the alert in the store and SSE stream while
# suppressing only its IM fan-out.
def test_ingest_notify_im_false_stores_and_publishes_without_im(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
) -> None:
    notified = capture_im(monkeypatch)

    with alert_frames(event_bus) as published, alerts_client() as client:
        resp = ingest(client, webhook(notify_im="false"))
    assert resp.json() == {"processed": 1, "inserted": 1, "updated": 0, "notified": 0}
    assert notified == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, notified_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
    assert row == ("unresolved", None)

    assert [frame["status"] for frame in published] == ["unresolved"]


# meiva:note
# A gated firing and its resolution both update the row without IM.
def test_ingest_notify_im_false_firing_and_resolution_stay_silent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    notified = capture_im(monkeypatch)

    with alerts_client() as client:
        firing = ingest(client, webhook(notify_im="false"))
        resolved = ingest(
            client,
            webhook(
                status="resolved",
                ends_at="2026-08-04T11:00:00Z",
                notify_im="false",
            ),
        )
    assert firing.json()["notified"] == 0
    assert resolved.json()["notified"] == 0
    assert notified == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, ends_at, notified_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
    assert row == ("resolved", datetime(2026, 8, 4, 11, 0, tzinfo=UTC), None)


# meiva:note
# Only the exact string "false" gates IM; "true" keeps the default.
def test_ingest_notify_im_non_gating_value_still_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified = capture_im(monkeypatch)

    with alerts_client() as client:
        resp = ingest(client, webhook(notify_im="true"))
    assert resp.json()["notified"] == 1
    assert len(notified) == 1


# meiva:note
# Gated still-firing re-sends and a new firing episode never retry IM.
def test_ingest_notify_im_false_resends_and_refire_stay_silent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    notified = capture_im(monkeypatch)

    with alerts_client() as client:
        firing = ingest(client, webhook(notify_im="false"))
        resend = ingest(client, webhook(notify_im="false"))
        resolved = ingest(
            client,
            webhook(
                status="resolved",
                ends_at="2026-08-04T11:00:00Z",
                notify_im="false",
            ),
        )
        refire = ingest(
            client,
            webhook(
                starts_at="2026-08-04T12:00:00Z",
                notify_im="false",
            ),
        )

    assert [response.json()["notified"] for response in (firing, resend, resolved, refire)] == [
        0,
        0,
        0,
        0,
    ]
    assert resend.json()["updated"] == 1
    assert refire.json()["inserted"] == 1
    assert notified == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, notified_at FROM alerts ORDER BY starts_at")
        rows = cur.fetchall()
    assert rows == [("resolved", None), ("unresolved", None)]


# meiva:note
# IM template language follows user_settings display.language — an "en"
# row selects the English template set through the full ingest path (the
# default zh path is covered by test_ingest_every_severity_notifies).
def test_ingest_uses_display_language_setting(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value) VALUES ('display.language', %s)",
            (Jsonb("en"),),
        )
    db_conn.commit()

    notified = capture_im(monkeypatch)
    with alerts_client() as client:
        ingest(client, webhook())
    assert len(notified) == 1
    assert (
        # meiva:note
        # emoji-ok: asserting the user-designated IM format
        "\u26a0\ufe0f ALERT [ERROR] test-rule" in notified[0]
    )


# meiva:note
# Alertmanager's zero time endsAt (0001-01-01T00:00:00Z) is NULL.
def test_ingest_zero_ends_at_stored_null(db_conn: psycopg.Connection) -> None:

    with alerts_client() as client:
        ingest(client, webhook(ends_at="0001-01-01T00:00:00Z"))

    with db_conn.cursor() as cur:
        cur.execute("SELECT ends_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


# meiva:note
# im_bridge down -> the ingest still stores the row (notified_at stays
# NULL) and answers 200.
def test_ingest_im_failure_does_not_fail_ingest(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_im(monkeypatch, ok=False)
    with alerts_client() as client:
        resp = ingest(client, webhook())
        assert resp.status_code == 200
        assert resp.json()["notified"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT notified_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


# meiva:note
# notified_at NULL keeps the firing gate open — the next re-send retries
# the IM.
def test_ingest_firing_retries_notify_after_failed_attempt(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_im(monkeypatch, ok=False)
    with alerts_client() as client:
        ingest(client, webhook())
    capture_im(monkeypatch, ok=True)
    with alerts_client() as client:
        resp = ingest(client, webhook())
        assert resp.json()["notified"] == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(notified_at) IS NOT NULL FROM alerts")
        row = cur.fetchone()
        assert row is not None
        n, stamped = row
    assert n == 1 and stamped


# meiva:note
# A resolved row that fires again (new starts_at) is a new episode — a
# fresh row + a fresh IM.
def test_ingest_refire_after_resolution_notifies_again(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook())
        ingest(client, webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"))
        resp = ingest(
            client,
            webhook(starts_at="2026-08-04T12:00:00Z", ends_at=""),
        )
        assert resp.json()["inserted"] == 1
        assert resp.json()["notified"] == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2


# meiva:note
# A payload without a fingerprint gets the Alertmanager-standard hash of
# its labels — stable across sends (dedup holds), and equal to Grafana's
# hash for the same label set.
def test_ingest_computes_fingerprint_when_absent(db_conn: psycopg.Connection) -> None:
    with alerts_client() as client:
        ingest(client, webhook(alerts=[alert(fingerprint="")]))
        resp = ingest(client, webhook(alerts=[alert(fingerprint="")]))
        assert resp.json()["inserted"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT fingerprint, count(*) FROM alerts GROUP BY fingerprint")
        row = cur.fetchone()
        assert row is not None
        fp, n = row
    assert n == 1
    from base.telemetry.alerts import fingerprint as compute_fp

    assert fp == compute_fp({"alertname": "test-rule", "team": "meiva-ops", "severity": "error"})


# meiva:note
# The full Alertmanager v4 envelope (version/groupKey/truncatedAlerts/
# receiver/groupLabels/commonLabels/commonAnnotations/externalURL) is
# accepted — only status + alerts[] matter to the store.
def test_ingest_alertmanager_v4_envelope_tolerated(db_conn: psycopg.Connection) -> None:
    payload = {
        "version": "4",
        "groupKey": '{}/{}:{{alertname="test-rule"}}',
        "truncatedAlerts": 0,
        "receiver": "meiva-alerts-webhook",
        "groupLabels": {"alertname": "test-rule"},
        "commonLabels": {"alertname": "test-rule"},
        "commonAnnotations": {"summary": "test summary"},
        "externalURL": "http://localhost:3002/",
        "status": "firing",
        "alerts": [alert()],
    }
    with alerts_client() as client:
        resp = ingest(client, payload)
        assert resp.status_code == 200
        assert resp.json()["inserted"] == 1


# meiva:note
# The slimmer Grafana-managed shape (top-level status only, no per-alert
# status) is still accepted — status falls back from the top level.
def test_ingest_grafana_flat_payload_tolerated(db_conn: psycopg.Connection) -> None:
    payload: dict[str, Any] = {
        "status": "firing",
        "alerts": [
            {
                "labels": {"alertname": "r", "severity": "warning"},
                "annotations": {"summary": "s"},
                "startsAt": "2026-08-04T10:00:00Z",
                "fingerprint": "gf1",
            }
        ],
    }
    with alerts_client() as client:
        resp = ingest(client, payload)
        assert resp.status_code == 200
        assert resp.json()["inserted"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "unresolved"


# meiva:note
# Every ingested row is published to the meiva:alerts Redis channel as one
# AlertRow JSON frame (the SSE stream's payload).
def test_ingest_publishes_sse_frames(event_bus: EventBus) -> None:

    with alert_frames(event_bus) as published, alerts_client() as client:
        ingest(
            client,
            webhook(alerts=[alert(fingerprint="p1"), alert(fingerprint="p2", alertname="r2")]),
        )
        # meiva:note
        # duplicate re-send — still publishes (update)
        ingest(client, webhook())

    assert len(published) == 3
    for frame in published:
        assert frame["id"] > 0
        assert frame["fingerprint"] in ("p1", "p2", "abc123")


# meiva:note
# -- auth --------------------------------------------------------------------


# meiva:note
# No/wrong token -> 401; correct token -> 200; the legacy header name
# no longer authenticates (removed with task #1173).
def test_ingest_requires_webhook_token(client: TestClient) -> None:
    payload = webhook()
    # meiva:note
    # the cluster secret is itself a valid webhook credential
    without_credentials(client)
    assert client.post("/api/alerts", json=payload).status_code == 401
    assert (
        client.post(
            "/api/alerts", json=payload, headers={"X-Ops-Alerts-Token": "test-token"}
        ).status_code
        == 401
    )
    assert (
        client.post("/api/alerts", json=payload, headers={"X-Alerts-Token": "wrong"}).status_code
        == 401
    )
    assert (
        client.post(
            "/api/alerts", json=payload, headers={"X-Alerts-Token": "test-token"}
        ).status_code
        == 200
    )


# meiva:note
# Cluster-secret Bearer is an alternative webhook credential.
def test_ingest_bearer_cluster_secret_accepted() -> None:
    payload = webhook()
    credential = "supersecret"
    with alerts_client(cluster_secret=credential) as client:
        resp = client.post(
            "/api/alerts",
            json=payload,
            headers={"Authorization": f"Bearer {credential}"},
        )
    assert resp.status_code == 200


# meiva:note
# The webhook token also works as a Bearer credential — Grafana 13
# webhook contact points can only authenticate via the notifier-native
# Authorization fields (custom headers are stored in plaintext by the 13
# provisioning schema), so the gateway accepts the scoped webhook token on
# the Bearer path.
def test_ingest_bearer_webhook_token_accepted(client: TestClient) -> None:
    payload = webhook()
    resp = client.post(
        "/api/alerts",
        json=payload,
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200


# meiva:note
# With no webhook token configured, loopback callers are trusted and
# remote ones rejected.
@pytest.mark.parametrize(
    ("host", "trusted"), [("127.0.0.1", True), ("::1", True), ("10.0.0.5", False)]
)
def test_ingest_loopback_trust_when_no_token(host: str, trusted: bool) -> None:
    with gateway_test_client(fields={"webhook_token": None}, client=(host, 5000)) as client:
        resp = without_credentials(client).post("/api/alerts", json=webhook())
    assert resp.status_code == (200 if trusted else 401)


# meiva:note
# A configured webhook token ends loopback trust: the caller must present it.
def test_ingest_loopback_is_not_enough_when_a_token_is_set() -> None:
    with alerts_client(client=("127.0.0.1", 5000)) as client:
        resp = without_credentials(client).post("/api/alerts", json=webhook())
    assert resp.status_code == 401


# meiva:note
# -- one IM per notification group --------------------------------------------


# meiva:note
# Native retries recover their original group rather than re-rendering expanded POSTs.
def test_native_groups_recover_original_group_and_new_members_without_sent_fact(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = capture_im(monkeypatch, ok=False)
    a, b, c = [alert(fingerprint=fp, summary=fp) for fp in ("a", "b", "c")]
    with alerts_client() as client:
        first = ingest(client, webhook(alerts=[a, b]))
        second = ingest(client, webhook(alerts=[a, b, c]))
        assert first.json() == {"processed": 2, "inserted": 2, "updated": 0, "notified": 0}
        assert second.json() == {"processed": 3, "inserted": 1, "updated": 2, "notified": 0}
    assert len(sent) == 3
    assert sent[0] == sent[1]
    # meiva:note
    # C is a distinct source operation.
    assert sent[2] != sent[0]
    rows = db_conn.execute(
        "SELECT text,origin FROM alert_notification_groups ORDER BY id"
    ).fetchall()
    assert rows[0] == (sent[0], "native-v1")
    assert rows[1] == (sent[2], "native-v1")
    assert db_conn.execute("SELECT count(*) FROM alert_notification_members").fetchone() == (3,)
    assert db_conn.execute(
        "SELECT count(*) FROM alerts WHERE notified_at IS NOT NULL"
    ).fetchone() == (0,)


# meiva:note
# A later entry still finds the same instance inserted earlier in this POST.
def test_shadow_keeps_input_order_missing_start_resolution(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = capture_im(monkeypatch, ok=False)
    first = alert(fingerprint="same")
    later = alert(fingerprint="same", starts_at="", summary="later")
    with alerts_client() as client:
        response = ingest(client, webhook(alerts=[first, later]))
    assert response.json() == {"processed": 2, "inserted": 1, "updated": 1, "notified": 0}
    assert len(sent) == 1
    # meiva:note
    # The original source body is immutable.
    assert "later" not in sent[0]
    assert db_conn.execute("SELECT count(*) FROM alert_notification_members").fetchone() == (1,)
    assert db_conn.execute("SELECT annotations->>'summary' FROM alerts").fetchone() == ("later",)
