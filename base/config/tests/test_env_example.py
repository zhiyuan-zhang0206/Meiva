from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values
from pydantic import AliasChoices

from base.config import FIELD_INFOS
from base.config._full import build
from base.host.env.dotenv_boot import EnvBootResult

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _env_example_values() -> dict[str, str]:
    values = dotenv_values(_REPO_ROOT / ".env.example")
    assert values, ".env.example parsed to nothing — file missing or parser broke"
    no_value_keys = [key for key, value in values.items() if value is None]
    assert not no_value_keys, f".env.example has keys without '=': {no_value_keys}"
    return {key: value for key, value in values.items() if value is not None}


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for field_name, field in FIELD_INFOS.items():
        names.add(field_name.upper())
        if field.alias:
            names.add(field.alias)
        if field.serialization_alias:
            names.add(field.serialization_alias)
        validation_alias = field.validation_alias
        if isinstance(validation_alias, str):
            names.add(validation_alias)
        elif isinstance(validation_alias, AliasChoices):
            names.update(choice for choice in validation_alias.choices if isinstance(choice, str))
    return names


def test_env_example_has_no_blank_typed_fields() -> None:
    typed_aliases = {
        f.alias: f.annotation
        for f in FIELD_INFOS.values()
        if f.alias and isinstance(f.annotation, type) and f.annotation in (int, bool, float)
    }

    offenders = [
        key for key, value in _env_example_values().items() if value == "" and key in typed_aliases
    ]

    assert not offenders, (
        f".env.example ships blank values for typed fields {offenders}: an empty "
        "int/bool/float fails parsing and aborts settings load on a fresh "
        "`cp .env.example` (the field default applies only when the var is absent)."
    )


def test_env_example_loads_as_settings() -> None:
    clean_env = dict.fromkeys(sorted(_settings_env_names()), "")
    for name in list(clean_env):
        if name not in _env_example_values():
            clean_env.pop(name, None)
    clean_env.update(_env_example_values())
    build(env_boot=EnvBootResult(), environment=clean_env)
