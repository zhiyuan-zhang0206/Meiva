# meiva:note
# The decisions a takeover attachment takes over values: which lease an attach call names, whether
# the held plugin-state version still fits the lease, how far the native graph already applied the
# journal, and whether the lease opens an event receipt.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import pytest

from meiva.impersonation.admission import (
    LeaseTarget,
    applied_version,
    lease_target,
    participant_source_key,
    require_held_version,
)


def test_a_string_names_the_lease_itself() -> None:
    assert lease_target("lease-1", None) == LeaseTarget("lease-1", None, None)


def test_an_integer_session_names_the_lease_through_its_agent() -> None:
    assert lease_target(7, 41) == LeaseTarget("", 41, 7)


@pytest.mark.parametrize(
    ("session", "agent"),
    [("lease-1", 41), (7, None), (True, 41), (False, None), ("lease-1", 0)],
)
def test_any_other_pair_is_refused(session: int | str, agent: int | None) -> None:
    with pytest.raises(ValueError, match="attach requires an agent_id and an integer session_id"):
        lease_target(session, agent)


def test_the_held_version_must_equal_the_leases() -> None:
    require_held_version(3, 3)
    with pytest.raises(RuntimeError, match="another attachment changed plugin state"):
        require_held_version(4, 3)


@pytest.mark.parametrize(
    ("lease_applied", "receipt_lease", "receipt_version", "expected"),
    [
        (0, None, 0, 0),
        (2, "other", 5, 2),
        (2, "lease-1", 1, 2),
        (2, "lease-1", 5, 5),
        (0, "lease-1", 1, 1),
    ],
)
def test_the_applied_version_is_the_furthest_receipt_naming_this_lease(
    lease_applied: int, receipt_lease: str | None, receipt_version: int, expected: int
) -> None:
    assert applied_version(lease_applied, receipt_lease, receipt_version, "lease-1") == expected


def test_a_log_native_lease_opens_a_receipt_keyed_by_process_and_suffix() -> None:
    assert participant_source_key(log_native=True, pid=77, suffix="abc") == "attachment:77:abc"


def test_a_lease_without_an_event_log_opens_no_receipt() -> None:
    assert participant_source_key(log_native=False, pid=77, suffix="abc") is None
