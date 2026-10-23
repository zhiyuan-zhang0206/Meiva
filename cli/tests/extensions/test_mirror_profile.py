"""The mirror profiles and how they load.

An operator exports `scripts/mirrors/NAME.env` into the shell that acquires
dependencies; a unit's existing `$MEIVA_HOME/mirror.env` is what
`base.host.env.dotenv_boot.load_meiva_env` loads after `.env`. These tests pin two things
the design leans on: the `cn` profile carries the
package-manager index/registry vars, and a mirror var loaded from the file never
overrides one already in the real environment (precedence: env > .env > mirror.env).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CN_PROFILE = _REPO_ROOT / "scripts" / "mirrors" / "cn.env"


def test_cn_profile_carries_the_package_manager_vars() -> None:
    """The cn profile sets exactly the index/registry env each package manager
    honors — PyPI (uv), npm, and Homebrew bottles + API."""
    values = dotenv_values(_CN_PROFILE)
    assert values["UV_DEFAULT_INDEX"], "PyPI index for uv sync"
    assert values["npm_config_registry"], "npm registry for npm ci"
    assert values["HOMEBREW_BOTTLE_DOMAIN"], "Homebrew bottle mirror (macOS)"
    assert values["HOMEBREW_API_DOMAIN"], "Homebrew formulae API mirror (macOS)"
    # Every value is a concrete https endpoint, not a placeholder.
    for key, val in values.items():
        assert val and val.startswith("https://"), f"{key} should be an https URL, got {val!r}"


# meiva:note
# A mirror var already exported (or set from .env) wins over mirror.env —
# the boot never overrides an already-set key, which is what gives the
# documented `env > .env > mirror.env` precedence.
def test_mirror_env_does_not_override_real_environment(tmp_path: Path) -> None:
    from base.host.env import dotenv_boot

    (tmp_path / "mirror.env").write_text("UV_DEFAULT_INDEX=https://mirror.example/simple\n")
    env = {"UV_DEFAULT_INDEX": "https://explicit.example/simple"}

    dotenv_boot.load_meiva_env(env, home=tmp_path)  # must NOT clobber the explicit value

    assert env["UV_DEFAULT_INDEX"] == "https://explicit.example/simple"


def test_mirror_env_fills_unset_keys(tmp_path: Path) -> None:
    """When a mirror var is not otherwise set, mirror.env supplies it."""
    from base.host.env import dotenv_boot

    (tmp_path / "mirror.env").write_text("npm_config_registry=https://registry.example\n")
    env: dict[str, str] = {}

    dotenv_boot.load_meiva_env(env, home=tmp_path)

    # npm reads the lowercase `npm_config_<key>` env form; the uppercase spelling
    # SIM112 prefers is not what npm honors, so the lowercase key is load-bearing.
    assert env["npm_config_registry"] == "https://registry.example"


@pytest.mark.parametrize("source_layer", ["environment", "unit"])
@pytest.mark.parametrize("higher_key", ["UV_INDEX_URL", "UV_DEFAULT_INDEX"])
def test_unit_boot_preserves_single_index_precedence_across_aliases(
    tmp_path: Path, source_layer: str, higher_key: str
) -> None:
    from base.host.env import dotenv_boot
    from cli._python_index import python_index

    lower_key = "UV_DEFAULT_INDEX" if higher_key == "UV_INDEX_URL" else "UV_INDEX_URL"
    unit = tmp_path / ".env"
    mirror = tmp_path / "mirror.env"
    mirror.write_text(f"{lower_key}=https://mirror.example/simple\n")
    env: dict[str, str] = {}
    if source_layer == "environment":
        env[higher_key] = "https://pypi.org/simple"
        unit.write_text(f"{lower_key}=https://unit.example/simple\n")
    else:
        unit.write_text(f"{higher_key}=https://pypi.org/simple\n")
    before = unit.read_bytes(), mirror.read_bytes()
    dotenv_boot.load_meiva_env(env, home=tmp_path)
    assert python_index(tmp_path, env) == "https://pypi.org/simple"
    assert (unit.read_bytes(), mirror.read_bytes()) == before
    env["UV_INDEX"] = "https://additional.example/simple"
    dotenv_boot.load_meiva_env(env, home=tmp_path)
    assert env["UV_INDEX"] == "https://additional.example/simple"
    with pytest.raises(ValueError, match="one default Python index"):
        python_index(tmp_path, env)
