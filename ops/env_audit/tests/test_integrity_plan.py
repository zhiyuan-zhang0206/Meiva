from __future__ import annotations

import pytest

from ops.env_audit.integrity_plan import (
    DigestlessHistory,
    EmptyHistory,
    History,
    Intact,
    NoHistory,
    RecordedHistory,
    Tampered,
    UnreadableHistory,
    is_armed,
    plan_integrity,
    recorded_history,
)

_DIGEST = "a" * 64
_OTHER = "b" * 64


def _recorded(keys: frozenset[str] = frozenset({"A"})) -> RecordedHistory:
    return RecordedHistory(digest=_DIGEST, keys_after=keys, official_site="ops_config_write")


# meiva:note
# A fresh home has neither the marker nor a history, so the guard leaves it alone; either one
# arms it.
def test_only_a_home_with_a_marker_or_a_history_is_armed() -> None:
    assert not is_armed(marker=False, history=NoHistory())
    assert is_armed(marker=True, history=NoHistory())
    assert is_armed(marker=False, history=EmptyHistory())
    assert is_armed(marker=False, history=_recorded())


# meiva:note
# Planning a home that is not armed is a caller bug, not a quiet pass.
def test_planning_an_unarmed_home_is_refused() -> None:
    with pytest.raises(ValueError, match="not armed"):
        plan_integrity(
            marker=False, history=NoHistory(), current_digest=_DIGEST, current_keys=frozenset()
        )


# meiva:note
# A file that matches its last record is intact, and a history that predates the marker is
# upgraded by writing it, not reported.
@pytest.mark.parametrize("marker", [True, False])
def test_a_matching_digest_is_intact(marker: bool) -> None:
    plan = plan_integrity(
        marker=marker, history=_recorded(), current_digest=_DIGEST, current_keys=frozenset({"A"})
    )

    assert plan == Intact(mark_armed=not marker)


# meiva:note
# An out-of-band edit is reported with the names whose presence changed since the last record
# (added and removed alike) and the official site the chain started from; the history is a
# sound append target, so it is not rebuilt.
def test_a_changed_digest_reports_the_keys_that_changed() -> None:
    plan = plan_integrity(
        marker=True,
        history=_recorded(frozenset({"A", "GONE"})),
        current_digest=_OTHER,
        current_keys=frozenset({"A", "NEW"}),
    )

    assert plan == Tampered(
        mark_armed=False,
        rebuild_history=False,
        reason=None,
        digest=_OTHER,
        last_official_site="ops_config_write",
        keys=("GONE", "NEW"),
        keys_after=("A", "NEW"),
    )


# meiva:note
# Each damaged history is its own named anomaly, rebuilt before the baseline is appended, with
# every current key reported because nothing can be compared with it.
@pytest.mark.parametrize(
    ("history", "reason"),
    [
        (NoHistory(), "audit_history_missing"),
        (UnreadableHistory(), "audit_history_corrupt"),
        (EmptyHistory(), "audit_history_empty"),
        (DigestlessHistory(), "audit_history_missing_digest"),
    ],
)
def test_an_armed_home_with_a_damaged_history_is_rebuilt(history: History, reason: str) -> None:
    plan = plan_integrity(
        marker=True, history=history, current_digest=_OTHER, current_keys=frozenset({"B", "A"})
    )

    assert plan == Tampered(
        mark_armed=False,
        rebuild_history=True,
        reason=reason,
        digest=_OTHER,
        last_official_site="",
        keys=("A", "B"),
        keys_after=("A", "B"),
    )


# meiva:note
# A damaged history in a home that only has a history (no marker yet) also asks for the marker.
def test_a_history_without_a_marker_asks_for_the_marker_when_it_is_damaged() -> None:
    plan = plan_integrity(
        marker=False, history=EmptyHistory(), current_digest=_OTHER, current_keys=frozenset()
    )

    assert isinstance(plan, Tampered) and plan.mark_armed


# meiva:note
# The last record's digest comes from `digest_after`, or from the older `digest`; a record with
# neither (or a non-string) cannot vouch for the file.
def test_the_last_record_names_its_digest_by_either_spelling() -> None:
    assert recorded_history({"digest_after": _DIGEST}) == RecordedHistory(_DIGEST, frozenset(), "")
    assert recorded_history({"digest": _DIGEST}).__class__ is RecordedHistory
    assert recorded_history({"site": "test"}) == DigestlessHistory()
    assert recorded_history({"digest_after": 7}) == DigestlessHistory()


# meiva:note
# The official site survives a chain of anomaly records, and a malformed key array counts as no
# names instead of a partial set.
def test_the_site_and_key_names_are_read_defensively() -> None:
    chained = recorded_history(
        {"digest": _DIGEST, "last_official_site": "converge_gateway_port", "keys_after": ["A", 1]}
    )
    sound = recorded_history({"digest_after": _DIGEST, "site": "test", "keys_after": ["A", "B"]})

    assert chained == RecordedHistory(_DIGEST, frozenset(), "converge_gateway_port")
    assert sound == RecordedHistory(_DIGEST, frozenset({"A", "B"}), "test")


# meiva:note
# The anomaly record, event and summary are shaped from the plan and a clock reading the shell
# supplies; a damaged history says why, and an out-of-band edit says where the chain started.
def test_the_anomaly_record_event_and_summary() -> None:
    edited = Tampered(
        mark_armed=False,
        rebuild_history=False,
        reason=None,
        digest=_OTHER,
        last_official_site="test",
        keys=("NEW",),
        keys_after=("A", "NEW"),
    )
    damaged = Tampered(
        mark_armed=False,
        rebuild_history=True,
        reason="audit_history_empty",
        digest=_OTHER,
        last_official_site="",
        keys=("A",),
        keys_after=("A",),
    )

    assert edited.record("t") == {
        "ts": "t",
        "kind": "unauthorized",
        "digest": _OTHER,
        "last_official_site": "test",
        "keys": ["NEW"],
        "keys_after": ["A", "NEW"],
    }
    assert damaged.record("t")["reason"] == "audit_history_empty"
    assert edited.event("m1") == {"machine": "m1", "last_official_site": "test", "keys": ["NEW"]}
    assert damaged.event("m1")["reason"] == "audit_history_empty"
    assert "official write at test" in edited.summary()
    assert "tamper detected: audit_history_empty" in damaged.summary()
