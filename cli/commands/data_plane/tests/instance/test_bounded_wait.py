# meiva:note
# The verdict of a bounded wait: settle on a held condition, give up at the deadline, else retry.

from __future__ import annotations

from cli.commands.data_plane.instance.bounded_wait import Wait, wait_verdict


def test_a_satisfied_condition_settles_even_with_the_budget_spent() -> None:
    assert wait_verdict(satisfied=True, elapsed_s=0.0, timeout_s=60.0) is Wait.DONE
    assert wait_verdict(satisfied=True, elapsed_s=99.0, timeout_s=60.0) is Wait.DONE


def test_an_unmet_condition_retries_until_the_deadline() -> None:
    assert wait_verdict(satisfied=False, elapsed_s=0.0, timeout_s=60.0) is Wait.RETRY
    assert wait_verdict(satisfied=False, elapsed_s=59.9, timeout_s=60.0) is Wait.RETRY


def test_an_unmet_condition_expires_at_the_deadline() -> None:
    assert wait_verdict(satisfied=False, elapsed_s=60.0, timeout_s=60.0) is Wait.EXPIRED
    assert wait_verdict(satisfied=False, elapsed_s=0.0, timeout_s=0.0) is Wait.EXPIRED
