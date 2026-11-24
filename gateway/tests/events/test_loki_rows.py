from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pydantic
import pytest

from base.events.reads import EventRecord
from gateway.events import loki_rows
from gateway.events.loki_query import LokiFilters
from gateway.tests.events.loki_stub import LokiStub, event_line

_NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
_FROM = _NOW - timedelta(hours=1)
_ALL = LokiFilters(categories=("telemetry", "log"))


def _query(stub: LokiStub, *, to: datetime | None = None, limit: int = 100) -> list[EventRecord]:
    client = loki_rows.build_client(stub.url, timeout_seconds=5)
    with client:
        rows, _ = loki_rows.query_events(client, _ALL, from_=_FROM, to=to, now=_NOW, limit=limit)
    return rows


def test_a_page_asks_for_one_extra_row_in_a_window_padded_by_a_microsecond() -> None:
    with LokiStub() as stub:
        _query(stub, limit=10)

    (sent,) = stub.script.queries
    assert sent["limit"] == "11"
    assert sent["direction"] == "backward"
    now_ns = int(_NOW.timestamp()) * 10**9
    assert int(sent["start"]) == now_ns - 3600 * 10**9 - 500
    assert int(sent["end"]) == now_ns + 500 + 1


def test_a_line_that_is_not_an_event_is_an_error_not_a_skipped_row() -> None:
    with LokiStub() as stub:
        stub.script.lines.append((int(_NOW.timestamp()) * 10**9 - 10**9, "not json"))
        with pytest.raises(pydantic.ValidationError):
            _query(stub)


def test_rows_come_back_newest_first_in_one_shape_whatever_the_stream() -> None:
    with LokiStub() as stub:
        for seconds, name in ((30, "old"), (10, "new")):
            stub.script.lines.append(event_line(_NOW - timedelta(seconds=seconds), name))
        rows = _query(stub)

    assert [row["event_name"] for row in rows] == ["new", "old"]
    assert rows[0]["ts"] == _NOW - timedelta(seconds=10)
    assert rows[0]["attributes"] == {}


def test_a_total_sums_the_series_and_rounds_its_range_up_to_a_millisecond() -> None:
    with LokiStub() as stub:
        stub.script.counts = [({"a": "1"}, 4), ({"a": "2"}, 3)]
        client = loki_rows.build_client(stub.url, timeout_seconds=5)
        with client:
            total = loki_rows.count_events(
                client, _ALL, from_=_NOW - timedelta(microseconds=2500), to=None, now=_NOW
            )

    assert total == 7
    (sent,) = stub.script.queries
    assert sent["query"].endswith("[3ms]))")
    assert int(sent["time"]) == int(_NOW.timestamp()) * 10**9


def test_groups_name_the_key_by_the_dimension_and_put_service_events_under_none() -> None:
    with LokiStub() as stub:
        stub.script.counts = [
            ({"agent_id": "7"}, 2),
            ({"agent_id": ""}, 5),
            ({"agent_id": "9"}, 2),
            ({"agent_id": "3"}, 0),
        ]
        client = loki_rows.build_client(stub.url, timeout_seconds=5)
        with client:
            groups = loki_rows.count_event_groups(
                client, _ALL, by="agent_id", from_=_FROM, to=None, now=_NOW, limit=2
            )

    assert groups == [(None, 5), ("7", 2)]


def test_the_machine_group_reads_the_body_label() -> None:
    with LokiStub() as stub:
        stub.script.counts = [({"event_machine": "m1"}, 2)]
        client = loki_rows.build_client(stub.url, timeout_seconds=5)
        with client:
            groups = loki_rows.count_event_groups(
                client, _ALL, by="machine", from_=_FROM, to=None, now=_NOW, limit=5
            )

    assert groups == [("m1", 2)]
