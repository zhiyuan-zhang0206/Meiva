from __future__ import annotations

import pytest

from base.agents.sdk import InertSamplingPolicy, SamplingPolicy, keep_sample
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner, sampling_owner_serving
from base.clock.tests.seeded_rng import seeded_rng


@pytest.mark.parametrize(
    ("every", "draw", "kept"),
    [
        (1, 0.99, True),
        (0, 0.99, True),
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
    rng = seeded_rng()
    state = rng.getstate()
    assert [fixed_sampling_owner().should_sample(1) for _ in range(5)] == [True] * 5
    assert rng.getstate() == state


def test_the_owner_draws_from_the_generator_its_root_gave_it(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    env_path = tmp_path_factory.mktemp("sampling") / ".env"
    owner = sampling_owner_serving(SamplingPolicy(sampling_enabled=True, sample_every=4), env_path)
    reference = seeded_rng()
    expected = [keep_sample(4, reference.random()) for _ in range(30)]
    assert [owner.should_sample(4) for _ in range(30)] == expected


def test_an_inert_policy_keeps_every_call_and_refuses_a_ratio() -> None:
    inert = InertSamplingPolicy()
    assert inert.should_sample(1) is True
    with pytest.raises(RuntimeError, match="never samples"):
        inert.should_sample(5)
