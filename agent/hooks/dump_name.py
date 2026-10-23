# meiva:note
# The file name of a pre-compact history dump, from values: `<start>__<end>.jsonl`, both UTC. Start is the earliest message read time (second precision; the write moment when no stamp parses), end is the write moment (microsecond precision, so two dumps never collide). Both stamps are fixed-width and a dump's start never decreases from one dump to the next, so name order is chronological order.
# meiva:link agent/hooks/docs/hooks.meiva.okf.md
from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AnyMessage

from base.agents.messages.kwargs import message_read_time


# meiva:note
# The earliest read time across the dumped messages, or None. Every message shape but the SystemMessage carries the stamp, so this is the moment the round's context began. An unparseable stamp is skipped rather than sinking the whole dump; a stamp without a timezone is read as UTC.
def earliest_read_time(messages: list[AnyMessage]) -> datetime | None:
    earliest: datetime | None = None
    for msg in messages:
        raw = message_read_time(msg)
        if raw is None:
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if earliest is None or ts < earliest:
            earliest = ts
    return earliest


def dump_filename(messages: list[AnyMessage], end: datetime) -> str:
    start = earliest_read_time(messages) or end
    return f"{start:%Y%m%dT%H%M%SZ}__{end:%Y%m%dT%H%M%S.%fZ}.jsonl"
