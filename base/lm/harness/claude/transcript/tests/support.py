from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.events import Usage
from base.lm.harness.session.transcript.drafts import (
    InboundPart,
    Projection,
    SentInput,
    TimelineDraft,
    UsageTally,
)
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.projection import Projector

FIXTURES = Path(__file__).parent / "fixtures"


def now_ns() -> int:
    return time.time_ns()


def _object(text: str) -> dict[str, object]:
    raw = cast("object", json.loads(text))
    assert is_object_dict(raw)
    return raw


def rows(scenario: str, record: str | None = None) -> list[dict[str, object]]:
    path = FIXTURES / scenario / (f"subagents/{record}.jsonl" if record else "main.jsonl")
    return [_object(line) for line in path.read_text().splitlines()]


def source(scenario: str) -> dict[str, object]:
    return _object((FIXTURES / scenario / "source.json").read_text())


def result_usage(scenario: str, index: int) -> Usage:
    results = source(scenario)["result_messages"]
    assert is_object_list(results)
    raw = results[index]
    assert is_object_dict(raw)

    def count(name: str) -> int:
        value = raw[name]
        assert isinstance(value, int)
        return value

    cache_write = count("cache_creation_input_tokens")
    cache_read = count("cache_read_input_tokens")
    return Usage(
        input_tokens=count("input_tokens") + cache_read + cache_write,
        output_tokens=count("output_tokens"),
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


def parse_all(
    raw_rows: Iterable[Mapping[str, object]],
    state: ParseState | None = None,
    *,
    first_line: int = 0,
) -> tuple[ParseState, list[AnyEntry]]:
    current = state or ParseState()
    entries: list[AnyEntry] = []
    for line, raw in enumerate(raw_rows, start=first_line):
        current, parsed = parse_row(current, raw, line=line)
        entries.extend(parsed)
    return current, entries


def entries_of(scenario: str, record: str | None = None) -> list[AnyEntry]:
    return parse_all(rows(scenario, record))[1]


def sent(wire_id: str, key: str, text: str, parts: Sequence[InboundPart] = ()) -> SentInput:
    return SentInput(
        key=key,
        wire_id=wire_id,
        text=text,
        parts=tuple(parts) or (InboundPart(ref=f"{key}-row", text=text),),
    )


def project(
    entries: Iterable[AnyEntry],
    inputs: Sequence[SentInput] = (),
    *,
    known_keys: Iterable[str] = (),
) -> tuple[Projector, Projection]:
    projector = Projector(known_keys=known_keys)
    for item in inputs:
        projector.register(item)
    return projector, projector.feed(entries)


def in_order(drafts: Iterable[TimelineDraft]) -> list[TimelineDraft]:
    return list(drafts)


def interleaved(scenario: str, records: Sequence[str]) -> list[AnyEntry]:
    tagged = [
        (raw["timestamp"], 0, line, raw)
        for line, raw in enumerate(rows(scenario))
        if "timestamp" in raw
    ]
    for index, record in enumerate(records, start=1):
        tagged += [
            (raw["timestamp"], index, line, raw)
            for line, raw in enumerate(rows(scenario, record))
            if "timestamp" in raw
        ]
    state = ParseState()
    entries: list[AnyEntry] = []
    for _, _, line, raw in sorted(tagged, key=lambda item: (str(item[0]), item[1], item[2])):
        state, parsed = parse_row(state, raw, line=line)
        entries.extend(parsed)
    return entries


def kinds(drafts: Iterable[TimelineDraft]) -> list[str]:
    return [draft.kind.value for draft in drafts]


def total(tallies: dict[str, UsageTally]) -> Usage:
    result = Usage()
    for tally in tallies.values():
        result = result + tally.usage
    return result
