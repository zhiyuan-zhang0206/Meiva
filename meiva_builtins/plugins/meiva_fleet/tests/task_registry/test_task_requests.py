from meiva_builtins.plugins.meiva_fleet._task_requests import (
    creation_request,
    effective_owner,
    update_request,
)

_UNCHANGED = object()


def test_an_omitted_owner_is_the_actor_and_an_explicit_one_stays() -> None:
    assert effective_owner(None, 5) == 5
    assert effective_owner(7, 5) == 7


def test_a_creation_request_keeps_an_omitted_reminder_interval_omitted() -> None:
    request = creation_request(
        title="t",
        description="d",
        parent=1,
        owner=5,
        priority="P2",
        remind_interval_seconds=None,
    )
    assert request == {
        "title": "t",
        "description": "d",
        "parent": 1,
        "owner": 5,
        "priority": "P2",
        "remind_interval_seconds": None,
    }


def test_an_update_request_leaves_out_what_was_not_passed() -> None:
    request = update_request(
        {
            "status": "done",
            "title": None,
            "owner": _UNCHANGED,
            "remind_interval_seconds": _UNCHANGED,
            "note": "",
        },
        unchanged=_UNCHANGED,
    )
    assert request == {"status": "done", "note": ""}


def test_an_explicit_root_parent_is_part_of_an_update_request() -> None:
    assert update_request({"parent_id": None}, unchanged=_UNCHANGED) == {"parent_id": None}
    assert update_request({"parent_id": _UNCHANGED}, unchanged=_UNCHANGED) == {}
