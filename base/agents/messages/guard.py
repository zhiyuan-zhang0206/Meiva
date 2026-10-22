from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import (
    AnyMessage,
    RemoveMessage,
    convert_to_messages,
)
from langchain_core.messages.utils import message_chunk_to_message
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from base.agents.messages.identity import normalize_stored_message_ids


# meiva:note
# Raised when a messages-channel mutation violates the append-only invariant (only a
# full wipe via `RemoveMessage(REMOVE_ALL)`, a tail append, or modifying the last
# message is allowed).
# meiva:link agent/messages/docs/messages.meiva.okf.md
class MessagesMutationError(RuntimeError):
    pass


# meiva:note
# Coerces message-likes the same way `add_messages` does so validation fingerprints
# compare the exact objects the reducer merged.
# meiva:link agent/messages/docs/messages.meiva.okf.md
def _coerce(messages: Any) -> list[AnyMessage]:
    return [cast(AnyMessage, message_chunk_to_message(m)) for m in convert_to_messages(messages)]


def _contains_remove_all(delta: Any) -> bool:
    msgs: list[Any] = cast(list[Any], delta) if isinstance(delta, list) else [delta]
    return any(isinstance(m, RemoveMessage) and m.id == REMOVE_ALL_MESSAGES for m in msgs)


# meiva:note
# A committed channel value never contains `RemoveMessage` markers; their presence in
# `current` means the hook runner is merging sibling deltas rather than validating a
# committed history.
# meiva:link agent/messages/docs/messages.meiva.okf.md
def _is_delta(current: Any) -> bool:
    msgs: list[Any] = cast(list[Any], current) if isinstance(current, list) else [current]
    return any(isinstance(m, RemoveMessage) for m in msgs)


def _fingerprint(m: AnyMessage) -> dict[str, Any]:
    return m.model_dump()


def _describe(m: AnyMessage) -> str:
    return f"{type(m).__name__} id={m.id!r}"


def _violation(before: list[AnyMessage], after: list[AnyMessage], detail: str) -> None:
    raise MessagesMutationError(
        f"messages-channel mutation violated the append-only invariant "
        f"(user ruling 2026-08-13, task #1256): only a full wipe "
        f"(RemoveMessage(REMOVE_ALL)), a tail append, or modifying the last "
        f"message is allowed. before={len(before)} messages, "
        f"after={len(after)} messages. Violation: {detail}"
    )


# meiva:note
# Append-only class check: no deletions, survivors stay at the same index and keep
# their content (except the last message), and new messages form a contiguous suffix.
# meiva:link agent/messages/docs/messages.meiva.okf.md
def validate_messages_mutation(before: Sequence[AnyMessage], after: Sequence[AnyMessage]) -> None:
    before = _coerce(before)
    after = _coerce(after)
    if not before:
        return
    before_by_id: dict[str, int] = {cast(str, m.id): i for i, m in enumerate(before)}
    last_before = len(before) - 1
    seen_new = False
    for i, m in enumerate(after):
        mid = m.id
        if mid in before_by_id:
            j = before_by_id[mid]
            if seen_new:
                _violation(
                    before,
                    after,
                    f"survivor {_describe(m)} at after index {i} follows a new "
                    f"message — new messages must form a tail suffix (no "
                    f"middle insertion / reordering)",
                )
            if j != i:
                _violation(
                    before,
                    after,
                    f"survivor {_describe(m)} moved from before index {j} to "
                    f"after index {i} — earlier messages must keep their "
                    f"position (no reorder / middle insert / deletion)",
                )
            if i != last_before and _fingerprint(m) != _fingerprint(before[j]):
                _violation(
                    before,
                    after,
                    f"message {_describe(m)} at index {i} changed content — "
                    f"only the last message (index {last_before}) may be "
                    f"modified",
                )
        else:
            seen_new = True
    after_ids = {m.id for m in after}
    missing = [m for m in before if m.id not in after_ids]
    if missing:
        _violation(
            before,
            after,
            f"messages deleted: {[_describe(m) for m in missing]} — only a "
            f"full wipe (RemoveMessage(REMOVE_ALL)) may delete messages",
        )


# meiva:note
# Full-wipe class check (`RemoveMessage(REMOVE_ALL)`): any surviving message carried
# over must keep its exact content and relative order.
# meiva:link agent/messages/docs/messages.meiva.okf.md
def validate_rebuild(before: Sequence[AnyMessage], after: Sequence[AnyMessage]) -> None:
    before = _coerce(before)
    after = _coerce(after)
    if not before:
        return
    before_by_id: dict[str, int] = {cast(str, m.id): i for i, m in enumerate(before)}
    after_idx: dict[str, int] = {cast(str, m.id): i for i, m in enumerate(after)}
    prev = -1
    for m in after:
        mid = m.id
        if mid not in before_by_id:
            continue
        j = before_by_id[mid]
        if _fingerprint(m) != _fingerprint(before[j]):
            _violation(
                before,
                after,
                f"full-wipe rebuild altered surviving message {_describe(m)} "
                f"(before index {j}) — old messages must survive a rebuild "
                f"unchanged",
            )
        if j < prev:
            _violation(
                before,
                after,
                f"full-wipe rebuild reordered surviving messages "
                f"({_describe(m)}: before index {j}, after index "
                f"{after_idx[mid]}) — survivors must keep their relative "
                f"order",
            )
        prev = j


# meiva:note
# Fast path for pure tail appends (no `RemoveMessage` and no id already present in
# `current`), threading `(messages, ids)` across writes in `O(N + K)` while falling
# back to `None` whenever missing/duplicate/remove shapes need the full diff check.
# meiva:link agent/messages/docs/messages.meiva.okf.md
def _try_pure_append(
    current: Any,
    delta: Any,
    cached: tuple[list[AnyMessage], set[str]] | None = None,
) -> tuple[list[AnyMessage], set[str]] | None:
    if current is None or delta is None:
        return None
    delta_msgs = _coerce(delta)
    if any(isinstance(m, RemoveMessage) for m in delta_msgs):
        return None
    delta_ids: list[str] = []
    for m in delta_msgs:
        if m.id is None:
            return None
        delta_ids.append(m.id)
    current_msgs: list[AnyMessage]
    current_ids: set[str]
    if cached is None:
        current_msgs = _coerce(current)
        current_ids = set()
        for m in current_msgs:
            if m.id is None:
                return None
            current_ids.add(m.id)
    else:
        current_msgs, current_ids = cached
    new_msgs: list[AnyMessage] = []
    first_slot: dict[str, int] = {}
    for m, mid in zip(delta_msgs, delta_ids, strict=True):
        if mid in current_ids:
            return None
        first = first_slot.get(mid)
        if first is None:
            first_slot[mid] = len(new_msgs)
            new_msgs.append(m)
        else:
            new_msgs[first] = m
    merged = [*current_msgs, *new_msgs]
    current_ids.update(first_slot)
    return merged, current_ids


# meiva:link agent/messages/docs/messages.meiva.okf.md
def guarded_add_messages(current: Any, delta: Any) -> Any:
    if _is_delta(current):
        return add_messages(current, delta)
    fast = _try_pure_append(current, delta)
    if fast is not None:
        return fast[0]
    merged: Any = add_messages(current, delta)
    before = _coerce(current)
    after = _coerce(merged)
    if _contains_remove_all(delta):
        validate_rebuild(before, after)
    else:
        validate_messages_mutation(before, after)
    return merged


# meiva:note
# Replays stored `DeltaChannel` writes in order through `guarded_add_messages` so
# validation stays batching-invariant when reads combine writes into larger batches.
# meiva:link agent/messages/docs/messages.meiva.okf.md
def guarded_delta_reducer(state: Any, writes: Sequence[Any]) -> Any:
    result: Any = state
    cached: tuple[list[AnyMessage], set[str]] | None = None
    markers = _is_delta(state)
    for write in writes:
        delta: list[Any] = cast(list[Any], write) if isinstance(write, list) else [write]
        if not markers:
            fast = _try_pure_append(result, delta, cached)
            if fast is not None:
                result = fast[0]
                cached = fast
                continue
        result = guarded_add_messages(
            normalize_stored_message_ids(result), normalize_stored_message_ids(delta)
        )
        cached = None
        markers = _is_delta(result)
    return result
