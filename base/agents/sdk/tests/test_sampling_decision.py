from __future__ import annotations

from pathlib import Path

import pytest

from base.agents.sdk import InertSamplingPolicy, keep_sample
from base.agents.sdk.call_policy import SamplingPolicyOwner, process_sampling_owner
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner, sampling_owner_over
from base.clock.tests.seeded_rng import seeded_rng
from base.paths.tests.homes import unit_home as unit_home


@pytest.mark.parametrize(
    ("every", "draw", "kept"),
    [
        (1, 0.99, True),
        (4, 0.0, True),
        (4, 0.249, True),
        (4, 0.25, False),
        (4, 0.99, False),
    ],
)
def test_a_call_is_kept_when_its_draw_falls_in_the_first_one_in_every(
    every: int, draw: float, kept: bool
) -> None:
    assert keep_sample(every, draw) is kept


# meiva:note
# Regression for the counter that replaced the random draw: a shared "every Nth call" counter keeps
# one call site for good and drops the other when two sites alternate. Sampling is per call and
# independent, so each of two alternating sites is kept about half the time at 1 in 2.
def test_two_alternating_call_sites_are_each_sampled() -> None:
    owner = fixed_sampling_owner()
    kept = [owner.should_sample(2) for _ in range(2000)]

    first_site, second_site = sum(kept[0::2]), sum(kept[1::2])

    assert 400 <= first_site <= 600
    assert 400 <= second_site <= 600


@pytest.mark.parametrize("every", [0, -3])
def test_a_ratio_that_is_not_a_positive_count_is_refused(every: int) -> None:
    with pytest.raises(ValueError, match="positive count"):
        keep_sample(every, 0.5)


@pytest.mark.parametrize("every", [0, -3])
def test_the_owner_refuses_a_bad_ratio_before_it_draws(every: int) -> None:
    rng = seeded_rng()
    state = rng.getstate()
    owner = sampling_owner_over(rng)

    with pytest.raises(ValueError, match="positive count"):
        owner.should_sample(every)

    assert rng.getstate() == state


def test_the_kept_share_is_one_in_every() -> None:
    owner = fixed_sampling_owner()

    kept = sum(owner.should_sample(5) for _ in range(20_000))

    assert 3_600 <= kept <= 4_400


def test_the_same_seed_makes_the_same_decisions() -> None:
    first, second = fixed_sampling_owner(), fixed_sampling_owner()
    assert [first.should_sample(3) for _ in range(50)] == [
        second.should_sample(3) for _ in range(50)
    ]


def test_an_every_call_policy_draws_nothing() -> None:
    rng = seeded_rng(0)
    state = rng.getstate()

    owner = sampling_owner_over(rng)

    assert [owner.should_sample(1) for _ in range(5)] == [True] * 5
    assert rng.getstate() == state


def _decisions(owner: SamplingPolicyOwner) -> list[bool]:
    return [owner.should_sample(4) for _ in range(60)]


@pytest.mark.parametrize("seed", [1, 2])
def test_the_owner_draws_from_the_generator_its_root_gave_it(seed: int) -> None:
    reference = seeded_rng(seed)
    expected = [keep_sample(4, reference.random()) for _ in range(60)]

    assert _decisions(sampling_owner_over(seeded_rng(seed))) == expected


def test_generators_with_different_seeds_make_different_decisions() -> None:
    assert _decisions(sampling_owner_over(seeded_rng(1))) != _decisions(
        sampling_owner_over(seeded_rng(2))
    )


def test_the_process_owner_keeps_the_generator_its_root_gave_it(unit_home: Path) -> None:
    rng = seeded_rng(4)

    owner = process_sampling_owner(rng)
    try:
        assert owner.rng is rng
    finally:
        owner.close()


def test_an_inert_policy_keeps_every_call_and_refuses_a_ratio() -> None:
    inert = InertSamplingPolicy()
    assert inert.should_sample(1) is True
    with pytest.raises(RuntimeError, match="never samples"):
        inert.should_sample(5)
    with pytest.raises(ValueError, match="positive count"):
        inert.should_sample(0)
