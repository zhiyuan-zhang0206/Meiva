# meiva:note
# Windowing a rendered timeline: which items a page holds and whether older history remains.
#
# The functions take one `Row` of facts per rendered item and return a `Selection`: the indices of
# the items the page holds, in order. Reading checkpoints, rendering and picking the items stay
# in `gateway.agents.history.timeline`, so the rules are asserted on plain values.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "Row",
    "Selection",
    "boundary_read_limit",
    "depth_allows",
    "initial_window",
    "older_segment_available",
    "window_before",
    "window_or_cross",
]

_REATTACHED_KINDS = frozenset({"system_prompt", "inbound_compact_summary"})


# meiva:note
# What the windowing rules read of one rendered timeline item.
#
# `tagged_plain` is true when the item carries a real source tag and no impersonation marker:
# the only markers that can be standing head notes.
@dataclass(frozen=True)
class Row:
    item_id: str
    kind: str
    tagged_plain: bool


# meiva:note
# The items a page holds, as indices into the rows it was planned from, oldest first.
#
# `has_more` says older history remains. `crossed` means everything before the cursor was
# re-attached context, so the caller appends the next older segment.
@dataclass(frozen=True)
class Selection:
    indices: tuple[int, ...]
    has_more: bool
    crossed: bool = False


# meiva:note
# How many compact boundaries a non-zero history depth reads, newest first.
#
# A positive depth reads one more than the depth, so a boundary beyond it shows that
# older history exists; depth -1 (unbounded) reads all of them (None).
def boundary_read_limit(depth: int) -> int | None:
    return depth + 1 if depth > 0 else None


def depth_allows(rank: int, depth: int) -> bool:
    return depth == -1 or 0 < rank <= depth


def older_segment_available(rank: int, boundary_count: int, depth: int) -> bool:
    older_rank = rank + 1
    return older_rank <= boundary_count and depth_allows(older_rank, depth)


# meiva:note
# Item ids of the standing head notes: the contiguous ``system_marker``
# run immediately after the system prompt.
#
# These are the notes ``agent/graph/prompt/context_notes.py`` lays down at window
# establishment (exec timeout / timezone / cluster memory / agent id / agent
# memory / preloaded skills), rendered as ``system_marker`` items right
# behind the prompt. They are standing context of the same class as the
# prompt itself: ``initial_window`` re-attaches them at the window head,
# and the paging paths treat them as re-attached context (a cursor that
# would select one must cross to the next older segment instead of looping
# on the re-attached head).
#
# The run requires ``source is not None`` (a real NoteTag): the catch-all
# fallback renders untagged HumanMessages — pre-tag checkpoint rows and
# retired markers that old data may still carry right after the prompt —
# as ``system_marker`` with ``source=None``, and treating those as head
# notes would re-attach stale rows to every window and skip them in paging.
def _standing_head_note_ids(items: Sequence[Row]) -> set[str]:
    head: set[str] = set()
    idx = 0
    # meiva:note
    # The current segment's prompt is always the plain "0.0" item (historical
    # segments carry segment-prefixed ids), and the standing notes directly
    # follow it. Historical segments have their SystemMessage stripped by
    # ``load_checkpoint_messages_segment``, so their standing notes sit at
    # items[0] — the run starts there when no prompt is present.
    while idx < len(items) and items[idx].kind != "system_prompt":
        idx += 1
    if idx < len(items):
        # meiva:note
        # past the prompt itself
        idx += 1
    else:
        # meiva:note
        # no prompt (historical segment): its notes start at the front
        idx = 0
    while idx < len(items) and items[idx].kind == "system_marker" and items[idx].tagged_plain:
        head.add(items[idx].item_id)
        idx += 1
    return head


# meiva:note
# Return up to `limit` items immediately older than the `before` item_id.
#
# `before` is the oldest item_id the frontend currently holds. If it is
# not found (rolled off / never existed), return an empty window with no
# more — the frontend stops asking rather than looping.
def window_before(items: Sequence[Row], before: str, limit: int) -> Selection:
    idx = next((i for i, it in enumerate(items) if it.item_id == before), None)
    if idx is None:
        return Selection((), has_more=False)
    start = max(0, idx - limit) if limit > 0 else 0
    return Selection(tuple(range(start, idx)), has_more=start > 0)


# meiva:note
# Return a segment-local page or signal that the cursor is at its head.
#
# The frontend never uses re-attached context as a cursor. When every item
# before its oldest real item is re-attached context (the prompt, the
# standing head notes, compact summaries), return that small head once more
# while signaling the caller to append the next older segment.
# Frontend de-duplication converges repeated standing context, and returning
# it here guarantees a compact summary cannot fall between page boundaries.
def window_or_cross(
    items: Sequence[Row], before: str, limit: int, *, older_available: bool
) -> Selection:
    idx = next((i for i, item in enumerate(items) if item.item_id == before), None)
    if idx is None:
        return Selection((), has_more=False)
    head_note_ids = _standing_head_note_ids(items)
    if all(item.kind in _REATTACHED_KINDS or item.item_id in head_note_ids for item in items[:idx]):
        return Selection(tuple(range(idx)), has_more=older_available, crossed=True)
    start = max(0, idx - limit) if limit > 0 else 0
    return Selection(tuple(range(start, idx)), has_more=start > 0 or older_available)


# meiva:note
# The standing context a tail window lost, in reading order: the system prompt (the oldest
# item), then the head notes, then the compact summaries.
#
# The standing head notes — the contiguous system_marker run right after the prompt (exec
# timeout / timezone / cluster memory / agent id / agent memory / preloaded skills,
# agent/graph/prompt/context_notes.py) — are the same class of standing context as the prompt:
# laid down at window establishment, they fall off the tail window for any conversation past
# `limit` rendered items. Re-attaching the missing ones right after the prompt makes a long
# conversation's head read like a fresh window (user report 2026-08-27: agent 2992's head
# showed only "system prompt · compact summary" while a fresh agent shows "system prompt · 2
# memories · 3 system notes"). Compact summaries are standing context too: once the prompt is
# re-attached, a cursor can never page to summaries older than it, so every missing summary
# follows the head notes. Items are deduped by item_id.
def _missing_standing_context(items: Sequence[Row], window: Sequence[int]) -> tuple[int, ...]:
    in_window = {items[i].item_id for i in window}
    prompt = (
        []
        if "0.0" in in_window
        else [i for i, item in enumerate(items) if item.kind == "system_prompt"][:1]
    )
    head_note_ids = _standing_head_note_ids(items)
    notes = [
        i
        for i, item in enumerate(items)
        if item.item_id in head_note_ids and item.item_id not in in_window
    ]
    compact = [
        i
        for i, item in enumerate(items)
        if item.kind == "inbound_compact_summary" and item.item_id not in in_window
    ]
    return (*prompt, *notes, *compact)


# meiva:note
# The head window of the current segment, preserving its standing context.
#
# The tail is the newest `tail_size` items and `tail_has_more` says whether older ones exist.
# What fell off the tail as standing context is re-attached at the head without counting against
# `limit` or creating a phantom older-page affordance.
def initial_window(
    items: Sequence[Row],
    *,
    tail_size: int,
    tail_has_more: bool,
    limit: int,
    historical_segments_available: bool,
) -> Selection:
    window = tuple(range(len(items) - tail_size, len(items)))
    has_more = tail_has_more
    reattached = _missing_standing_context(items, window)
    if reattached:
        window = (*reattached, *window)
        has_more = has_more and len(items) - len(reattached) > limit
    return Selection(window, has_more=has_more or historical_segments_available)
