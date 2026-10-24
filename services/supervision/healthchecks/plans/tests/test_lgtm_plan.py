from __future__ import annotations

import pytest

from services.supervision.healthchecks.plans.lgtm_plan import (
    Accepted,
    Failed,
    PushStep,
    Retry,
    marker_visible,
    probe_payload,
    push_step,
    query_params,
    readiness_failure,
)

_BACKOFF = (1.0, 2.0)


@pytest.mark.parametrize(
    ("status", "body", "attempt", "expected"),
    [
        (204, b"", 0, Accepted()),
        (200, b"", 2, Accepted()),
        (429, b"", 0, Retry(sleep_s=1.0)),
        (429, b"", 1, Retry(sleep_s=2.0)),
        (429, b"", 2, Failed("push_http_429")),
        (400, b"", 0, Failed("push_http_400")),
        (503, b"disk usage is too high", 0, Failed("push_http_503")),
        (500, b"desc = Ingester Is Shutting Down", 0, Failed("ingester_shutting_down")),
        (400, b"ingester is shutting down", 0, Failed("push_http_400")),
    ],
)
def test_a_push_answer_is_accepted_retried_once_per_pause_or_failed(
    status: int, body: bytes, attempt: int, expected: PushStep
) -> None:
    assert push_step(status=status, body=body, attempt=attempt, backoff_s=_BACKOFF) == expected


@pytest.mark.parametrize(
    ("name", "status", "database", "expected"),
    [
        ("loki", 200, None, None),
        ("loki", 503, None, "loki readiness HTTP 503"),
        ("grafana", 200, "ok", None),
        ("grafana", 200, "failed", "Grafana database is not ready"),
        ("grafana", 500, "ok", "grafana readiness HTTP 500"),
    ],
)
def test_a_readiness_answer_certifies_a_backend_or_names_why_not(
    name: str, status: int, database: str | None, expected: str | None
) -> None:
    assert readiness_failure(name=name, status=status, grafana_database=database) == expected


def test_the_probe_payload_carries_its_marker_and_timestamp() -> None:
    marker, body = probe_payload(42)

    assert marker == "watchdog-write-probe-42"
    assert b'"timeUnixNano": "42"' in body
    assert marker.encode() in body


def test_the_query_window_looks_back_from_the_marker_to_a_fresh_end() -> None:
    params = query_params(marker="m", marker_ns=500_000_000_000, query_end_ns=501_000_000_000)

    assert params["query"].endswith('|= "m"')
    assert params["start"] == "380000000000"
    assert params["end"] == "501000000000"


def test_the_marker_is_visible_only_as_a_streamed_value() -> None:
    found = {"data": {"result": [{"values": [["1", "other"], ["2", "m"]]}]}}

    assert marker_visible(found, "m") is True
    assert marker_visible({"data": {"result": []}}, "m") is False
