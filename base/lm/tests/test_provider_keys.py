from typing import Any, cast

import pytest

from base.lm.catalog.provider_keys import ProviderKeys


def test_a_key_is_required_by_its_env_name() -> None:
    keys = ProviderKeys({"FOO_API_KEY": "sk-foo"})
    assert keys.present("FOO_API_KEY")
    assert keys.require("FOO_API_KEY") == "sk-foo"


def test_a_missing_or_empty_key_fails_at_once() -> None:
    keys = ProviderKeys({"EMPTY_API_KEY": ""})
    assert not keys.present("EMPTY_API_KEY")
    with pytest.raises(RuntimeError, match="EMPTY_API_KEY not set"):
        keys.require("EMPTY_API_KEY")
    with pytest.raises(RuntimeError, match="OTHER_API_KEY not set"):
        keys.require("OTHER_API_KEY")


def test_the_environment_wins_over_the_env_file() -> None:
    keys = ProviderKeys.read(
        ("A_API_KEY", "B_API_KEY", "C_API_KEY"),
        environ={"A_API_KEY": "live", "C_API_KEY": ""},
        env_file={"A_API_KEY": "file", "B_API_KEY": "file-b", "UNDECLARED_API_KEY": "x"},
    )
    assert keys.require("A_API_KEY") == "live"
    assert keys.require("B_API_KEY") == "file-b"
    assert not keys.present("C_API_KEY")
    assert not keys.present("UNDECLARED_API_KEY")


def test_the_held_keys_are_a_copy_and_the_repr_hides_the_secrets() -> None:
    source = {"FOO_API_KEY": "sk-secret"}
    keys = ProviderKeys(source)
    source["FOO_API_KEY"] = "changed"
    assert keys.require("FOO_API_KEY") == "sk-secret"
    assert "sk-secret" not in repr(keys)
    assert "FOO_API_KEY" in repr(keys)
    with pytest.raises(TypeError):
        cast(Any, keys.secrets)["BAR_API_KEY"] = "x"
