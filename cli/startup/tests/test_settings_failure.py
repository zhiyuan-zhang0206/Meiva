# meiva:note
# How a Settings failure on an unconfigured home reads.

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from cli.startup.settings_failure import settings_failure_text


# meiva:note
# A ValidationError whose missing locs are `fields` (Python attribute names).
def _validation_error(*fields: str) -> ValidationError:
    model = type("_RequiredFields", (BaseModel,), {"__annotations__": dict.fromkeys(fields, str)})
    try:
        model()
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError for missing required fields")


def test_the_missing_fields_become_the_aliases_the_env_file_takes() -> None:
    lines = settings_failure_text(_validation_error("db_url", "MEIVA_REDIS_URL")).split("\n")

    assert "Missing env vars:" in lines
    assert lines[lines.index("Missing env vars:") + 1 :][:2] == [
        "  MEIVA_DB_URL=<value>",
        "  MEIVA_REDIS_URL=<value>",
    ]


def test_the_template_ends_with_the_first_start_hint_for_a_runner() -> None:
    text = settings_failure_text(_validation_error("db_url"))

    assert text.startswith("\n✗ meiva: failed to load settings")
    assert "meiva init --serve-agent-runner --no-serve-gateway" in text.splitlines()[-1]


def test_an_error_that_names_no_field_lists_no_variable() -> None:
    error = ValidationError.from_exception_data("Settings", [])

    assert "Missing env vars:" not in settings_failure_text(error)
