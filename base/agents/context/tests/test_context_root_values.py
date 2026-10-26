from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest

from base.agents.context import MeivaContext
from base.clock.tests.seeded_rng import seeded_rng


# meiva:note
# A generator whose draws the test can reproduce with another one built from the same seed.
def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


def test_a_context_without_a_user_home_refuses_to_read_one() -> None:
    with pytest.raises(RuntimeError, match="no user home"):
        MeivaContext().require_user_home()


def test_a_context_hands_back_the_user_home_its_root_recorded(tmp_path: Path) -> None:
    assert MeivaContext(user_home=tmp_path).require_user_home() == tmp_path


def test_a_context_without_a_meiva_home_refuses_to_read_one() -> None:
    with pytest.raises(RuntimeError, match="no Meiva home"):
        MeivaContext().require_meiva_home()


def test_the_meiva_home_and_the_user_home_are_recorded_apart(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    meiva_home = tmp_path / "meiva"
    context = MeivaContext(user_home=user_home, meiva_home=meiva_home)

    assert context.require_user_home() == user_home
    assert context.require_meiva_home() == meiva_home


def test_a_context_without_a_random_generator_refuses_to_read_one() -> None:
    with pytest.raises(RuntimeError, match="no random generator"):
        MeivaContext().require_rng()


def test_a_context_hands_back_the_generator_its_root_recorded() -> None:
    rng = seeded_rng()

    assert MeivaContext(rng=rng).require_rng() is rng


def test_a_context_without_a_generator_refuses_to_mint_an_identifier() -> None:
    with pytest.raises(RuntimeError, match="no random generator"):
        MeivaContext().require_new_id()


def test_identifiers_are_version_4_uuids_drawn_from_the_roots_generator() -> None:
    context = MeivaContext(rng=_rng(3))
    reference = _rng(3)

    minted = [context.require_new_id() for _ in range(3)]

    assert minted == [str(UUID(int=reference.getrandbits(128), version=4)) for _ in range(3)]
    assert len(set(minted)) == 3
    assert all(UUID(value).version == 4 for value in minted)


def test_generators_with_different_seeds_mint_different_identifiers() -> None:
    first = MeivaContext(rng=_rng(1)).require_new_id()
    second = MeivaContext(rng=_rng(2)).require_new_id()

    assert first != second
