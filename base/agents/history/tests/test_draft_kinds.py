from __future__ import annotations

from typing import get_args

from base.agents.history.timeline_item import TimelineItem
from base.lm.harness.session.transcript.drafts import DraftKind


def test_every_transcript_draft_kind_is_a_timeline_item_kind() -> None:
    kind_field = TimelineItem.model_fields["kind"].annotation
    assert {kind.value for kind in DraftKind} <= set(get_args(kind_field))
