from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from agent.hooks.dump_name import dump_filename, earliest_read_time

_END = datetime(2026, 8, 13, 12, 0, 0, 7, tzinfo=UTC)


def _stamped(stamp: str) -> HumanMessage:
    return HumanMessage(content="m", additional_kwargs={"meiva_created_at": stamp})


def test_the_name_is_the_earliest_read_time_and_the_write_moment() -> None:
    messages: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        _stamped("2026-08-13T09:30:00+00:00"),
        _stamped("2026-08-13T08:15:00+00:00"),
    ]

    assert dump_filename(messages, _END) == "20260813T081500Z__20260813T120000.000007Z.jsonl"


def test_a_history_without_a_usable_stamp_starts_at_the_write_moment() -> None:
    messages: list[AnyMessage] = [_stamped("not-a-date"), SystemMessage(content="<sys>")]

    assert dump_filename(messages, _END) == "20260813T120000Z__20260813T120000.000007Z.jsonl"


def test_a_stamp_without_a_timezone_is_read_as_utc_and_a_bad_one_is_skipped() -> None:
    messages: list[AnyMessage] = [_stamped("2026-08-13T07:00:00"), _stamped("garbage")]

    assert earliest_read_time(messages) == datetime(2026, 8, 13, 7, 0, tzinfo=UTC)


def test_names_sort_in_write_order() -> None:
    messages: list[AnyMessage] = [_stamped("2026-08-13T08:15:00+00:00")]
    later = datetime(2026, 8, 13, 12, 0, 0, 8, tzinfo=UTC)

    assert dump_filename(messages, _END) < dump_filename(messages, later)
