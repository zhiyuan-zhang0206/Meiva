import pytest

from agent.ownership.audit_payloads import admitted_payload, reaped_payload, settled_payload


@pytest.mark.parametrize("previous", ["idling", "running"])
def test_an_admission_audits_the_status_the_row_was_in(previous: str) -> None:
    assert admitted_payload(previous) == {"from": previous, "to": "running"}


def test_a_settle_audits_running_to_idling() -> None:
    assert settled_payload() == {"from": "running", "to": "idling"}


@pytest.mark.parametrize("owed", [False, True])
def test_a_reaped_corpse_audits_whether_it_still_owed_a_handoff_receipt(owed: bool) -> None:
    assert reaped_payload(handoff_unapplied=owed) == {
        "from": "idling",
        "to": "terminated",
        "reason": "corpse_reaper",
        "unapplied_handoff_receipt": owed,
    }
