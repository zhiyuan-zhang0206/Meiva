from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from services.backup.artifact import passphrase


def _minted() -> str:
    return passphrase.mint(random.SystemRandom())


def test_a_minted_passphrase_is_64_lowercase_hex_characters_of_256_bits() -> None:
    rng = random.SystemRandom()
    draws = [passphrase.mint(rng) for _ in range(8)]

    assert all(re.fullmatch(r"[0-9a-f]{64}", draw) for draw in draws)
    assert len(set(draws)) == len(draws)
    # meiva:note
    # A narrower draw padded to 64 digits stays below 250 bits; 256 random bits exceed 250 bits
    # except with probability 2**-6 per draw, so eight draws all missing is 2**-48.
    assert any(int(draw, 16).bit_length() > 250 for draw in draws)


def test_birth_pins_the_value_it_is_given(tmp_path: Path) -> None:
    value = _minted()

    passphrase.ensure_pinned(tmp_path, passphrase=value)

    assert passphrase.resolve(tmp_path) == value


def test_an_interrupted_birth_keeps_the_passphrase_it_already_pinned(tmp_path: Path) -> None:
    first, second = _minted(), _minted()
    passphrase.ensure_pinned(tmp_path, passphrase=first)

    passphrase.ensure_pinned(tmp_path, passphrase=second)

    assert passphrase.resolve(tmp_path) == first


def test_birth_refuses_a_value_that_is_not_a_passphrase(tmp_path: Path) -> None:
    too_short = "short"

    with pytest.raises(passphrase.PassphrasePinError, match="64 lowercase hex"):
        passphrase.ensure_pinned(tmp_path, passphrase=too_short)


def test_birth_refuses_a_malformed_value_even_when_a_passphrase_is_already_pinned(
    tmp_path: Path,
) -> None:
    pinned = _minted()
    passphrase.ensure_pinned(tmp_path, passphrase=pinned)
    too_short = "short"

    with pytest.raises(passphrase.PassphrasePinError, match="64 lowercase hex"):
        passphrase.ensure_pinned(tmp_path, passphrase=too_short)

    assert passphrase.resolve(tmp_path) == pinned
