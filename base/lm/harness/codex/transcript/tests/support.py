from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.jsonl import read_jsonl_rows
from base.host.boundary.untyped import object_of
from base.lm.harness.session.transcript.drafts import (
    InboundPart,
    Projection,
    SentInput,
    TimelineDraft,
)
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.projection import Projector

FIXTURES = Path(__file__).parent / "fixtures"

CHILD_THREAD = "01a1cefe-574e-79f2-a985-5e66bedfa3a6"
SPAWN_CALL = "exec-19f89067-c493-4f06-a970-a5a4cb660f7f"
LATE_COMMAND = "exec-0b106fcd-871d-47a6-98d7-9f43278b35b5"

type Obj = dict[str, object]


def as_obj(value: object) -> Obj:
    assert is_object_dict(value)
    return value


def as_list(value: object) -> list[object]:
    assert is_object_list(value)
    return value


def load_json(relative: str) -> Obj:
    return as_obj(object_of(json.loads, (FIXTURES / relative).read_text()))


def load_lines(relative: str) -> list[Obj]:
    return read_jsonl_rows(FIXTURES / relative)


def page_rows(page: Obj) -> list[Obj]:
    return [as_obj(row) for row in as_list(page["data"])]


def row_item(row: Obj) -> Obj:
    return as_obj(row["item"])


def thread_turns(thread: Obj) -> list[Obj]:
    return [as_obj(turn) for turn in as_list(as_obj(thread["thread"])["turns"])]


def turn_items(turn: Obj) -> list[Obj]:
    return [as_obj(item) for item in as_list(turn["items"])]


def sent(wire_id: str, text: str, *, key: str | None = None) -> SentInput:
    ref = f"inbound-{wire_id}"
    return SentInput(key or f"input-{wire_id}", wire_id, text, (InboundPart(ref, text),))


def project(
    entries: Iterable[AnyEntry], *inputs: SentInput, known_keys: Iterable[str] = ()
) -> tuple[Projector, Projection]:
    projector = Projector(known_keys=known_keys)
    for one in inputs:
        projector.register(one)
    return projector, projector.feed(entries)


def in_order(projection: Projection) -> list[TimelineDraft]:
    return sorted(projection.drafts, key=lambda draft: (draft.order, draft.slot))


def kinds(projection: Projection) -> list[str]:
    return [draft.kind.value for draft in in_order(projection)]
