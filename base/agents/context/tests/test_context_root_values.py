from __future__ import annotations

import random
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from base.agents.context import MeivaContext
from base.clock.tests.seeded_rng import seeded_rng

# meiva:note
# A canary standing in for a real credential: the assertions below fail if any value the context
# holds reaches its printed form. It is never a real key, so a failing run is safe to paste.
_CANARY = "sk-CANARY-DO-NOT-LEAK"


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


def test_identifiers_are_distinct_32_character_version_4_uuids() -> None:
    context = MeivaContext(rng=_rng(3))

    minted = [context.require_new_id() for _ in range(3)]

    assert len(set(minted)) == 3
    assert all(len(value) == 32 and UUID(hex=value).version == 4 for value in minted)


def test_identifiers_come_from_the_generator_the_root_recorded() -> None:
    rng = _rng(3)
    before = rng.getstate()

    MeivaContext(rng=rng).require_new_id()

    assert rng.getstate() != before


def test_generators_with_the_same_seed_mint_the_same_identifiers() -> None:
    first = MeivaContext(rng=_rng(3))
    second = MeivaContext(rng=_rng(3))

    assert [first.require_new_id() for _ in range(3)] == [second.require_new_id() for _ in range(3)]


def test_generators_with_different_seeds_mint_different_identifiers() -> None:
    first = MeivaContext(rng=_rng(1)).require_new_id()
    second = MeivaContext(rng=_rng(2)).require_new_id()

    assert first != second


# meiva:note
# A context is printed routinely and it holds this process's whole environment: the repr names
# each field and its kind, so no value a secret rides in reaches a log line. A handle prints its
# own type, never its repr; a collection prints how many entries it holds.
def test_the_repr_shows_what_a_field_is_and_never_what_it_holds() -> None:
    class _Model:
        def __repr__(self) -> str:
            return _CANARY

    context = MeivaContext(
        llm=cast("Any", _Model()),
        meiva_home=Path("/meiva-home"),
        environ={
            "ANTHROPIC_API_KEY": _CANARY,
            "MEIVA_DB_URL": f"postgres://user:{_CANARY}@db/meiva_main",
        },
    )

    printed = repr(context)

    assert _CANARY not in printed
    assert "llm=<_Model>" in printed
    assert "environ=<2 entries>" in printed
    assert "meiva_home=PosixPath('/meiva-home')" in printed
