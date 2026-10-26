from __future__ import annotations

import random
from pathlib import Path

import pytest

from services.backup.artifact import passphrase


# meiva:note
# A generator with a fixed seed, so a test sees the same draw on every run.
def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


def test_a_minted_passphrase_is_64_lowercase_hex_characters_that_follow_the_generator() -> None:
    first = passphrase.mint(_rng(1))

    assert len(first) == 64 and first == first.lower()
    int(first, 16)
    assert passphrase.mint(_rng(1)) == first
    assert passphrase.mint(_rng(2)) != first


def test_birth_pins_the_value_it_is_given(tmp_path: Path) -> None:
    value = passphrase.mint(_rng(3))

    passphrase.ensure_pinned(tmp_path, passphrase=value)

    assert passphrase.resolve(tmp_path) == value


def test_an_interrupted_birth_keeps_the_passphrase_it_already_pinned(tmp_path: Path) -> None:
    first, second = passphrase.mint(_rng(4)), passphrase.mint(_rng(5))
    passphrase.ensure_pinned(tmp_path, passphrase=first)

    passphrase.ensure_pinned(tmp_path, passphrase=second)

    assert passphrase.resolve(tmp_path) == first


def test_birth_refuses_a_value_that_is_not_a_passphrase(tmp_path: Path) -> None:
    too_short = "short"

    with pytest.raises(passphrase.PassphrasePinError, match="64 lowercase hex"):
        passphrase.ensure_pinned(tmp_path, passphrase=too_short)
