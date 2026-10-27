# meiva:note
# `meiva/sdk_surface/pure/plans/installation/lifecycle_plan.py` unit tests: what a plugin load
# carries over from the installation it replaces, the refusal of a first load with no sampling
# owner, and the failure a rollback ends with. Asserted by value; the loads and the rollbacks
# themselves are covered by the install tests.

from __future__ import annotations

from dataclasses import replace

import pytest

from base.agents.sdk import InertSamplingPolicy
from base.clock import Clock
from base.clock.tests.fakes import system_clock
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.pure.plans.installation.lifecycle_plan import carry_over, rollback_outcome


class _Pipelines:
    def event_pipeline(self) -> object:
        return object()


def _previous(
    sampling: InertSamplingPolicy, clients: _Pipelines | None, clock: Clock | None
) -> Installation:
    return replace(Installation.bare(sampling=sampling), clients=clients, clock=clock)


def test_a_load_carries_over_what_the_replaced_installation_held() -> None:
    sampling, clients, clock = InertSamplingPolicy(), _Pipelines(), system_clock()
    carried = carry_over(
        _previous(sampling, clients, clock), sampling=None, clients=None, clock=None
    )
    assert (carried.sampling, carried.clients, carried.clock) == (sampling, clients, clock)


def test_what_the_caller_names_wins_over_the_replaced_installation() -> None:
    old = _previous(InertSamplingPolicy(), _Pipelines(), system_clock())
    sampling, clients, clock = InertSamplingPolicy(), _Pipelines(), system_clock()
    carried = carry_over(old, sampling=sampling, clients=clients, clock=clock)
    assert carried.sampling is sampling
    assert carried.clients is clients
    assert carried.clock is clock


def test_each_field_is_carried_over_on_its_own() -> None:
    old_sampling, old_clients = InertSamplingPolicy(), _Pipelines()
    clock = system_clock()
    carried = carry_over(
        _previous(old_sampling, old_clients, None), sampling=None, clients=None, clock=clock
    )
    assert carried.sampling is old_sampling
    assert carried.clients is old_clients
    assert carried.clock is clock


def test_a_first_load_keeps_what_it_is_given() -> None:
    sampling = InertSamplingPolicy()
    carried = carry_over(None, sampling=sampling, clients=None, clock=None)
    assert carried.sampling is sampling
    assert carried.clients is None
    assert carried.clock is None


def test_a_first_load_without_a_sampling_owner_is_refused() -> None:
    with pytest.raises(ValueError, match="needs the sampling owner its root retains"):
        carry_over(None, sampling=None, clients=_Pipelines(), clock=system_clock())


def test_a_primary_failure_stays_primary_and_collects_the_cleanup_failures_as_notes() -> None:
    primary = KeyError("plugin declaration")
    outcome = rollback_outcome(primary, [ValueError("a"), OSError("b")])
    assert outcome is None
    assert primary.__notes__ == [
        "SDK rollback also failed: ValueError: a",
        "SDK rollback also failed: OSError: b",
    ]


def test_a_primary_failure_with_a_clean_rollback_carries_no_notes() -> None:
    primary = KeyError("plugin declaration")
    assert rollback_outcome(primary, []) is None
    assert not hasattr(primary, "__notes__")


def test_with_no_primary_the_first_cleanup_failure_is_raised_with_the_later_ones_as_notes() -> None:
    first, second, third = ValueError("a"), OSError("b"), RuntimeError("c")
    outcome = rollback_outcome(None, [first, second, third])
    assert outcome is first
    assert first.__notes__ == [
        "SDK rollback also failed: OSError: b",
        "SDK rollback also failed: RuntimeError: c",
    ]
    assert not hasattr(second, "__notes__")


def test_a_lone_cleanup_failure_is_raised_as_it_is() -> None:
    only = ValueError("a")
    assert rollback_outcome(None, [only]) is only
    assert not hasattr(only, "__notes__")


def test_a_clean_rollback_with_no_primary_is_success() -> None:
    assert rollback_outcome(None, []) is None
