# meiva:note
# What the identity of the home's startup configuration is made of, asserted as values: which files
# take part and in what order, how each is keyed and hashed and how the digests combine. No file is
# read; `cli/commands/lifecycle/tests/startup/test_start_generation.py` digests real homes.

from __future__ import annotations

from pathlib import Path

from base.deploy.release.start_digest import (
    bytes_digest,
    candidate_paths,
    combined_digest,
    relative_key,
)

_HOME = Path("/units/main")
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_EMPTY_OBJECT_SHA256 = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


def test_the_fixed_files_come_first_then_the_agent_configs_sorted() -> None:
    configs = [
        _HOME / "configs" / "b" / "config.json",
        _HOME / "configs" / "a" / "config.json",
        _HOME / "configs" / "10" / "config.json",
    ]

    assert candidate_paths(_HOME, configs) == [
        _HOME / ".env",
        _HOME / "plugins_config.json",
        _HOME / "configs" / "10" / "config.json",
        _HOME / "configs" / "a" / "config.json",
        _HOME / "configs" / "b" / "config.json",
    ]


def test_a_home_with_no_agent_configs_has_the_two_fixed_files() -> None:
    assert candidate_paths(_HOME, []) == [_HOME / ".env", _HOME / "plugins_config.json"]


def test_a_file_is_keyed_by_its_path_below_the_home() -> None:
    assert relative_key(_HOME, _HOME / "configs" / "a" / "config.json") == "configs/a/config.json"
    assert relative_key(_HOME, _HOME / ".env") == ".env"


def test_the_digest_of_bytes_is_their_sha256() -> None:
    assert bytes_digest(b"") == _EMPTY_SHA256
    assert bytes_digest(b"a") != bytes_digest(b"b")


def test_the_combined_digest_of_nothing_is_the_digest_of_an_empty_object() -> None:
    assert combined_digest({}) == _EMPTY_OBJECT_SHA256


def test_the_combined_digest_does_not_depend_on_the_order_files_were_listed() -> None:
    forward = {".env": "1", "plugins_config.json": "2"}
    backward = {"plugins_config.json": "2", ".env": "1"}

    assert combined_digest(forward) == combined_digest(backward)


def test_a_missing_file_is_not_an_empty_one_and_each_file_counts() -> None:
    empty = bytes_digest(b"")

    assert combined_digest({".env": None}) != combined_digest({".env": empty})
    assert combined_digest({".env": empty}) != combined_digest({"plugins_config.json": empty})
    assert combined_digest({".env": "1"}) != combined_digest({".env": "2"})
