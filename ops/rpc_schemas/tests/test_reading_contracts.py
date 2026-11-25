from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from base.api_contracts.reading import (
    ReadingDirection,
    RecordReadingLocator,
    encode_reading_locator,
)
from ops.rpc_schemas import HarnessTimelineReadRequest, HarnessTimelineWindow


class StrictOldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int
    harness: str
    limit: int
    before: int | None
    explicit_visibility: bool
    delivery_mode: str | None


@pytest.mark.parametrize("before", [None, 100])
def test_ordinary_wire_is_consumed_by_a_strict_old_reader(before: int | None) -> None:
    request = HarnessTimelineReadRequest(
        agent_id=42,
        harness="codex",
        limit=50,
        before=before,
        explicit_visibility=False,
        delivery_mode=None,
    )
    wire = request.wire_payload()
    old = StrictOldRequest.model_validate(wire)
    assert old.before == before and old.delivery_mode is None
    assert not {
        "anchor_item_id",
        "reading_locator",
        "reading_direction",
        "floor",
        "session_id",
    }.intersection(wire)
    response = HarnessTimelineWindow(items=[], has_more=False, size=0).model_dump(mode="json")
    assert response == {
        "items": [],
        "has_more": False,
        "size": 0,
        "anchor_item_id": None,
        "anchor_applied": False,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"reading_locator": "bad"},
        {"reading_locator": None, "reading_direction": ReadingDirection.ANCHOR},
        {"before": 1, "anchor_item_id": "h0.0"},
        {"before": 1, "reading_direction": ReadingDirection.AFTER},
        {"anchor_item_id": "h0.0", "reading_direction": ReadingDirection.BEFORE},
    ],
)
def test_incomplete_and_mixed_selector_combinations_fail_at_the_typed_boundary(
    changes: dict[str, object],
) -> None:
    token = encode_reading_locator(
        RecordReadingLocator(agent_id=42, harness="codex", session_id="s", offset=0, block=0)
    )
    data = {
        "agent_id": 42,
        "harness": "codex",
        "limit": 50,
        "explicit_visibility": False,
        "delivery_mode": None,
        "reading_locator": token,
        **changes,
    }
    with pytest.raises(ValidationError):
        _ = HarnessTimelineReadRequest.model_validate(data)


def test_new_selector_wire_requires_a_reader_that_supports_it() -> None:
    token = encode_reading_locator(
        RecordReadingLocator(agent_id=42, harness="codex", session_id="s", offset=0, block=0)
    )
    request = HarnessTimelineReadRequest(
        agent_id=42,
        harness="codex",
        limit=50,
        reading_locator=token,
        reading_direction=ReadingDirection.ANCHOR,
        explicit_visibility=False,
        delivery_mode=None,
    )
    assert request.wire_payload()["reading_direction"] == "anchor"
    with pytest.raises(ValidationError):
        _ = StrictOldRequest.model_validate(request.wire_payload())


def test_a_floor_and_a_session_are_sent_only_when_asked_for() -> None:
    request = HarnessTimelineReadRequest(
        agent_id=42,
        harness="codex",
        limit=50,
        before=900,
        floor=100,
        session_id="s-1",
        explicit_visibility=False,
        delivery_mode=None,
    )
    assert (request.wire_payload()["floor"], request.wire_payload()["session_id"]) == (100, "s-1")


@pytest.mark.parametrize(
    "fields", [{"before": 50, "floor": 100}, {"anchor_item_id": "h5.0", "floor": 1}]
)
def test_a_floor_applies_only_to_a_window_that_ends_above_it(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HarnessTimelineReadRequest.model_validate(
            {
                "agent_id": 42,
                "harness": "codex",
                "limit": 50,
                "explicit_visibility": False,
                "delivery_mode": None,
                **fields,
            }
        )
