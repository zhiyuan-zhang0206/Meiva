from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from base.agents.context import MeivaContext


def test_a_context_without_a_user_home_refuses_to_read_one() -> None:
    with pytest.raises(RuntimeError, match="no user home"):
        MeivaContext().require_user_home()


def test_a_context_hands_back_the_user_home_its_root_recorded(tmp_path: Path) -> None:
    assert MeivaContext(user_home=tmp_path).require_user_home() == tmp_path


def test_a_context_without_an_identifier_source_refuses_to_mint() -> None:
    with pytest.raises(RuntimeError, match="no identifier source"):
        MeivaContext().require_new_id()


def test_every_identifier_comes_from_the_source_the_root_recorded() -> None:
    counter = itertools.count(1)
    context = MeivaContext(new_id=lambda: f"id-{next(counter)}")

    assert [context.require_new_id() for _ in range(3)] == ["id-1", "id-2", "id-3"]
