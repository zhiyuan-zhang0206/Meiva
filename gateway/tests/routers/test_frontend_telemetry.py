"""POST /api/frontend-telemetry — frontend user-modeling telemetry ingestion.

Locks the ingest contract: a valid batch lands one `frontend_interaction`
event (category=telemetry, source=user) per accepted interaction in the
unified stream; malformed batches fail fast (422), oversized bodies 413, and
the per-session rate limit backstop drops excess events (204 + warning log)
instead of letting a misbehaving tab blow up the table.

Runs on meiva_test: POST a batch through TestClient while a capturing event
pipeline is bound, then assert the events it received (the capture settles
the emitter's drain thread before it is read).
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import ValidationError

from base.telemetry import Event
from base.telemetry.tests.capture.event_capture import capturing_events
from gateway.routers import frontend_telemetry as ft_router
from gateway.schemas import FrontendTelemetryBatch
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client

# One valid interaction as the browser sends it.
PAGE = "fleet"
ELEMENT = "spawn"
SESSION = "123e4567-e89b-12d3-a456-426614174000"


def _payload(events: list[dict[str, str]], session: str = SESSION) -> dict[str, object]:
    return {"session_id": session, "events": events}


def _one(page: str = PAGE, element: str = ELEMENT, **extra: str) -> dict[str, str]:
    return {"page": page, "element": element, **extra}


type _Row = tuple[str, str, str, str, dict[str, object]]


# meiva:note
# A gateway test client with a capturing event pipeline bound after the app started. The yielded
# list is filled when the block ends, once the capture has settled, so a test reads it after the
# `with`.
@contextmanager
def _ingest() -> Generator[tuple[GatewayTestClient, list[Event]]]:
    with gateway_test_client() as client, capturing_events() as events:
        seen: list[Event] = []
        try:
            yield client, seen
        finally:
            seen.extend(events)


def _rows(seen: list[Event], session: str) -> list[_Row]:
    """This session's `frontend_interaction` events, oldest first."""
    out: list[_Row] = []
    for event in seen:
        attributes: dict[str, object] = dict(event.attributes)
        if event.event_name != "frontend_interaction" or attributes.get("session_id") != session:
            continue
        out.append((event.event_name, event.category, event.level, event.source, attributes))
    return out


def _session() -> str:
    import uuid

    return str(uuid.uuid4())


class TestFrontendTelemetryIngest:
    def test_valid_batch_lands_events(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        with _ingest() as (client, seen):
            r = client.post(
                "/api/frontend-telemetry",
                json=_payload(
                    [
                        _one(),
                        _one(element="page-view", page="control/config"),
                        _one(
                            element="setting-change",
                            page="control/display",
                            key="display.show_timestamp_weekday",
                            value="false",
                        ),
                    ],
                    session=session,
                ),
            )
        assert r.status_code == 204
        rows = _rows(seen, session)
        assert len(rows) == 3
        for row in rows:
            assert row[0] == "frontend_interaction"
            assert row[1] == "telemetry"
            assert row[2] == "info"
            assert row[3] == "user"
        attrs = [row[4] for row in rows]  # psycopg decodes jsonb to dict
        assert attrs[0] == {"page": "fleet", "element": "spawn", "session_id": session}
        assert attrs[1] == {
            "page": "control/config",
            "element": "page-view",
            "session_id": session,
        }
        assert attrs[2] == {
            "page": "control/display",
            "element": "setting-change",
            "session_id": session,
            "key": "display.show_timestamp_weekday",
            "value": "false",
        }

    def test_missing_key_value_omitted_not_null(self) -> None:  # type: ignore[no-untyped-def]
        """Non-setting events must not carry key/value at all."""
        session = _session()
        with _ingest() as (client, seen):
            r = client.post(
                "/api/frontend-telemetry",
                json=_payload([_one(element="composer-send")], session=session),
            )
        assert r.status_code == 204
        attrs = _rows(seen, session)[0][4]
        assert "key" not in attrs
        assert "value" not in attrs

    def test_malformed_batch_422(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        cases = [
            # empty events list
            _payload([]),
            # bad element charset (free text / spaces)
            _payload([_one(element="click me!")]),
            # bad page charset (path with query)
            _payload([_one(page="/fleet?x=1")]),
            # bad session id
            _payload([_one()], session="not-a-uuid!"),
            # key too long
            _payload([_one(element="setting-change", key="k" * 200)]),
            # value too long (free text smuggled as a value)
            _payload([_one(element="setting-change", value="v" * 200)]),
        ]
        with _ingest() as (client, seen):
            for body in cases:
                r = client.post("/api/frontend-telemetry", json=body)
                assert r.status_code == 422, body
                assert r.json()["code"] == "invalid_telemetry_batch"
        # nothing landed
        assert _rows(seen, session) == []

    def test_oversized_body_413(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        with _ingest() as (client, seen):
            r = client.post(
                "/api/frontend-telemetry",
                content=b"x" * (ft_router.MAX_BODY_BYTES + 1),
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 413
        assert r.json()["code"] == "telemetry_batch_too_large"
        assert _rows(seen, session) == []

    def test_rate_limit_backstop_drops_excess(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """Over the per-session budget: accepted up to the cap, the rest
        dropped — the event stream cannot be flooded by one tab."""
        session = _session()
        events = [_one(element=f"e{i}") for i in range(ft_router.MAX_EVENTS_PER_MINUTE + 10)]
        with _ingest() as (client, seen):
            r = client.post("/api/frontend-telemetry", json=_payload(events, session=session))
        assert r.status_code == 204
        rows = _rows(seen, session)
        assert len(rows) == ft_router.MAX_EVENTS_PER_MINUTE

    def test_second_session_gets_own_budget(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        other = _session()
        with _ingest() as (client, seen):
            r1 = client.post(
                "/api/frontend-telemetry",
                json=_payload([_one()] * (ft_router.MAX_EVENTS_PER_MINUTE + 5), session=session),
            )
            r2 = client.post(
                "/api/frontend-telemetry",
                json=_payload([_one()] * 3, session=other),
            )
        assert r1.status_code == 204
        assert r2.status_code == 204
        assert len(_rows(seen, session)) == ft_router.MAX_EVENTS_PER_MINUTE
        assert len(_rows(seen, other)) == 3


def _viewport_wire_batch() -> dict[str, Any]:
    return json.loads("""{
  "session_id": "123e4567-e89b-12d3-a456-426614174000",
  "events": [
    {
      "page": "home",
      "element": "viewport-anomaly",
      "ts": 1794889580247,
      "geometry": {
        "layout_height": 1000,
        "visual_height": 1000,
        "visual_scale": 1,
        "html_inline_height": 807,
        "html_height": 807,
        "body_height": 807,
        "shell_height": 807,
        "shell_bottom": 807,
        "composer_height": 50,
        "composer_bottom": 807,
        "incident": 1,
        "phase": "anomaly",
        "reason": "box-change",
        "elapsed_ms": 0.09999990463256836
      }
    },
    {
      "page": "home",
      "element": "viewport-anomaly",
      "ts": 1794889580513,
      "geometry": {
        "layout_height": 1000,
        "visual_height": 1000,
        "visual_scale": 1,
        "html_inline_height": null,
        "html_height": 1000,
        "body_height": 1000,
        "shell_height": 1000,
        "shell_bottom": 1000,
        "composer_height": 50,
        "composer_bottom": 1000,
        "incident": 1,
        "phase": "recovered",
        "reason": "box-change",
        "elapsed_ms": 266.19999980926514
      }
    }
  ]
}""")


def test_browser_batch_lands_complete_geometry() -> None:
    batch = _viewport_wire_batch()
    session = _session()
    batch["session_id"] = session
    with _ingest() as (client, seen):
        response = client.post("/api/frontend-telemetry", json=batch)
    assert response.status_code == 204
    rows = _rows(seen, session)
    assert len(rows) == 2
    for row, event in zip(rows, batch["events"], strict=True):
        assert row[4] == {
            "page": "home",
            "element": "viewport-anomaly",
            "session_id": session,
            **{f"geometry_{key}": value for key, value in event["geometry"].items()},
        }
    assert rows[0][4]["geometry_html_inline_height"] == 807
    assert rows[1][4]["geometry_phase"] == "recovered"


@pytest.mark.parametrize(
    "field,value",
    [
        ("incident", 4),
        ("incident", True),
        ("phase", "arbitrary"),
        ("reason", "https://private"),
        ("layout_height", "1000"),
        ("layout_height", float("inf")),
        ("html_height", -1),
        ("shell_bottom", -100_001),
        ("visual_scale", 0),
        ("elapsed_ms", 86_400_001),
        ("extra", "private text"),
    ],
)
def test_invalid_geometry_rejected(field: str, value: object) -> None:
    batch = _viewport_wire_batch()
    batch["events"][0]["geometry"][field] = value
    with pytest.raises(ValidationError):
        FrontendTelemetryBatch.model_validate(batch)


@pytest.mark.parametrize(
    "change",
    ["wrong-event", "wrong-page", "missing-geometry", "scalar", "unknown-field", "too-many"],
)
def test_geometry_restricted_to_fixed_event(change: str) -> None:
    batch = _viewport_wire_batch()
    event = batch["events"][0]
    if change == "wrong-event":
        event["element"] = "page-view"
    elif change == "wrong-page":
        event["page"] = "fleet"
    elif change == "missing-geometry":
        del event["geometry"]
    elif change == "scalar":
        event["value"] = "private"
    elif change == "unknown-field":
        event["text"] = "private"
    else:
        batch["events"] = [event] * 201
    with pytest.raises(ValidationError):
        FrontendTelemetryBatch.model_validate(batch)
