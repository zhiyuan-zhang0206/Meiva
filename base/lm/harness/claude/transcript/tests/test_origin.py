# meiva:note
# Who a row says put it into the conversation is information the parser keeps and the projection decides on, in one
# order for a user row and for a queued command alike: an input whose wire id we registered is ours and settles
# whatever origin the row names (a CLI that tags our own inputs with an origin we have never seen must not get them
# sent twice), else an origin the vocabulary knows is a notice, else a row with no origin (or `human`) is an input
# we did not send, else the origin is an anomaly that is reported and not shown.
# meiva:link base/lm/harness/docs/transcript-projection.md

from __future__ import annotations

from collections.abc import Callable

import pytest

from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.events import HarnessCause
from base.lm.harness.session.transcript.drafts import (
    DraftKind,
    InboundPart,
    MatchBasis,
    SentInput,
)
from base.lm.harness.session.transcript.entries import AnomalyKind, HumanInput
from base.lm.harness.session.transcript.projection import Projector

_TASK = "<task-notification><task-id>b1</task-id><status>completed</status></task-notification>"
KINDS = ("task-notification", "something-new")


def _queued(
    origin_kind: str | None, *, text: str = _TASK, source: str = "s-1"
) -> dict[str, object]:
    attachment: dict[str, object] = {
        "type": "queued_command",
        "source_uuid": source,
        "prompt": text,
    }
    if origin_kind is not None:
        attachment["origin"] = {"kind": origin_kind}
    return {
        "type": "attachment",
        "uuid": "u-1",
        "timestamp": "2026-11-12T10:00:00.000Z",
        "attachment": attachment,
    }


def _user(origin_kind: str | None, *, text: str = _TASK, uuid: str = "u-2") -> dict[str, object]:
    row: dict[str, object] = {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-11-12T10:00:00.000Z",
        "isSidechain": False,
        "promptId": "p-1",
        "message": {"role": "user", "content": text},
    }
    if origin_kind is not None:
        row["origin"] = {"kind": origin_kind}
    return row


def _entry(raw: dict[str, object]) -> HumanInput:
    _, (entry,) = parse_row(ParseState(), raw, line=3)
    assert isinstance(entry, HumanInput)
    return entry


def _ours(wire_id: str) -> SentInput:
    return SentInput(
        key="row:1", wire_id=wire_id, text="hello", parts=(InboundPart(ref="row:1", text="hello"),)
    )


def test_the_parser_keeps_the_origin_a_row_names_and_decides_nothing_about_it() -> None:
    for build in (_queued, _user):
        known, unknown = _entry(build("task-notification")), _entry(build("something-new"))
        assert known.origin is not None and known.origin.cause is HarnessCause.BACKGROUND_TASK
        assert known.origin.text.startswith("Background task b1")
        assert unknown.origin is not None and unknown.origin.cause is HarnessCause.UNKNOWN
        assert _entry(build(None)).origin is None and _entry(build("human")).origin is None
    assert _entry(_queued("task-notification", source="s-9")).wire_id == "s-9"
    assert _entry(_user("something-new", uuid="u-9")).wire_id == "u-9"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("build", [_queued, _user])
def test_an_input_of_ours_is_settled_by_its_wire_id_whatever_origin_the_row_names(
    kind: str, build: Callable[..., dict[str, object]]
) -> None:
    raw = build(kind, text="hello")
    entry = _entry(raw)
    projector = Projector()
    projector.register(_ours(str(entry.wire_id)))
    projection = projector.feed([entry])
    assert [(d.input_key, d.basis) for d in projection.deliveries] == [
        ("row:1", MatchBasis.WIRE_ID)
    ]
    assert [d.kind for d in projection.drafts] == [DraftKind.INBOUND_CHAT]
    assert not projection.anomalies and not projection.unmatched_inputs


@pytest.mark.parametrize("build", [_queued, _user])
def test_an_origin_the_vocabulary_knows_is_a_notice_and_an_unknown_one_is_reported_and_not_shown(
    build: Callable[..., dict[str, object]],
) -> None:
    known = Projector().feed([_entry(build("task-notification"))])
    assert [(d.kind, d.payload) for d in known.drafts] == [
        (DraftKind.SYSTEM_MARKER, "Background task b1 completed.")
    ]
    assert not known.deliveries and not known.unmatched_inputs and not known.anomalies

    unknown = Projector().feed([_entry(build("something-new"))])
    assert unknown.drafts == () and not unknown.deliveries and not unknown.unmatched_inputs
    assert [(a.anomaly, a.detail) for a in unknown.anomalies] == [
        (AnomalyKind.UNKNOWN_TYPE, "origin/something-new")
    ]


@pytest.mark.parametrize("origin", [None, "human"])
def test_a_row_with_no_origin_or_human_that_is_not_ours_is_an_unmatched_input(
    origin: str | None,
) -> None:
    for build in (_queued, _user):
        projection = Projector().feed([_entry(build(origin, text="hello"))])
        assert [d.kind for d in projection.drafts] == [DraftKind.INBOUND_CHAT]
        assert len(projection.unmatched_inputs) == 1 and not projection.anomalies
