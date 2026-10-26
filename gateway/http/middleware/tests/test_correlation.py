# meiva:note
# Which correlation id an error envelope carries: values in, the id out.

from __future__ import annotations

import pytest

from gateway.http.middleware.correlation import Correlation, correlation


def test_the_active_trace_id_wins_over_the_request_and_a_fresh_id() -> None:
    assert correlation(active="a" * 32, assigned="b" * 32, fresh="c" * 32) == Correlation(
        "a" * 32, assign=False
    )


def test_a_request_keeps_the_id_it_already_carries() -> None:
    assert correlation(active=None, assigned="b" * 32, fresh="c" * 32) == Correlation(
        "b" * 32, assign=False
    )


def test_a_request_without_either_takes_the_fresh_id_and_remembers_it() -> None:
    assert correlation(active=None, assigned=None, fresh="c" * 32) == Correlation(
        "c" * 32, assign=True
    )
    assert correlation(active=None, assigned=17, fresh="c" * 32) == Correlation(
        "c" * 32, assign=True
    )


def test_a_request_without_any_id_and_nothing_drawn_for_it_is_refused() -> None:
    with pytest.raises(ValueError, match="no correlation id"):
        correlation(active=None, assigned=None, fresh=None)


def test_a_request_that_carries_an_id_needs_nothing_drawn_for_it() -> None:
    assert correlation(active=None, assigned="b" * 32, fresh=None) == Correlation(
        "b" * 32, assign=False
    )
