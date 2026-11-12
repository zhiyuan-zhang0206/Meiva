from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.lm.harness.antigravity.transcript.parse import (
    ParseResult,
    TrajectorySide,
    parse_rows,
)
from base.lm.harness.session.transcript.drafts import (
    InboundPart,
    SentInput,
    TimelineDraft,
)
from base.lm.harness.session.transcript.entries import AnyEntry

FIXTURES = Path(__file__).parent / "fixtures"


# meiva:note
# One recorded scenario: its `rows.jsonl` (a reconstruction of `transcript_full.jsonl`, see its
# `source.json`), the trajectory-only side input when the scenario has one, and the child conversation's
# rows when it has a sub-agent. `source.json` is the provenance and is read for the facts a test checks
# against (the usage the ACP prompt returned).
@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    conversation_id: str
    rows: list[object]
    side: TrajectorySide
    child_rows: list[object]
    source: dict[str, object]

    @property
    def child_id(self) -> str:
        child = self.source["child_conversation_id"]
        assert isinstance(child, str)
        return child


def _json(path: Path) -> object:
    return object_of(json.loads, path.read_text())


def _object(path: Path) -> dict[str, object]:
    raw = _json(path)
    assert is_object_dict(raw), path
    return raw


def _lines(path: Path) -> list[object]:
    return [object_of(json.loads, line) for line in path.read_text().splitlines() if line.strip()]


def _text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _side(path: Path) -> TrajectorySide:
    if not path.exists():
        return TrajectorySide()
    raw = _object(path)
    ids, steps = raw["client_message_ids"], raw["interrupting_steps"]
    assert is_object_dict(ids) and is_object_list(steps)
    return TrajectorySide(
        client_message_ids={int(key): _text(value) for key, value in ids.items()},
        interrupting_steps=frozenset(step for step in steps if isinstance(step, int)),
    )


def load(name: str) -> Scenario:
    directory = FIXTURES / name
    source = _object(directory / "source.json")
    child_path = directory / "child.jsonl"
    return Scenario(
        name=name,
        conversation_id=_text(source["conversation_id"]),
        rows=_lines(directory / "rows.jsonl"),
        side=_side(directory / "side.json"),
        child_rows=_lines(child_path) if child_path.exists() else [],
        source=source,
    )


def parse(scenario: Scenario, *, side: bool = True) -> ParseResult:
    return parse_rows(
        scenario.rows,
        conversation_id=scenario.conversation_id,
        side=scenario.side if side else None,
    )


def parse_child(scenario: Scenario) -> ParseResult:
    return parse_rows(
        scenario.child_rows,
        conversation_id=scenario.child_id,
        record=scenario.child_id,
    )


def sent(key: str, wire_id: str, text: str, *, images: tuple[str, ...] = ()) -> SentInput:
    return SentInput(
        key=key,
        wire_id=wire_id,
        text=text,
        parts=(InboundPart(ref=f"{key}-part", text=text, images=images),),
    )


def kinds(drafts: tuple[TimelineDraft, ...]) -> list[str]:
    return [draft.kind.value for draft in drafts]


def keys_of(entries: tuple[AnyEntry, ...]) -> list[str]:
    return [entry.key for entry in entries]
