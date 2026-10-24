from __future__ import annotations

import pytest

from ops.env_audit.redaction import (
    AliasMeta,
    FieldDeclaration,
    RedactedChange,
    alias_metadata,
    process_label,
    redact_changes,
)

_METADATA = {
    "MEIVA_MODEL": AliasMeta("cluster-default", False),
    "ANTHROPIC_API_KEY": AliasMeta("cluster-default", True),
}


def _declaration(
    name: str, *, alias: str | None = None, sensitive: bool | int | float | str | None = False
) -> FieldDeclaration:
    return FieldDeclaration(
        name=name, alias=alias or name.upper(), scope="cluster-default", sensitive=sensitive
    )


# meiva:note
# Aliases come from the declarations, each with the scope and sensitivity it declares.
def test_aliases_project_from_their_declarations() -> None:
    projected = alias_metadata(
        [_declaration("llm_model", alias="MEIVA_MODEL"), _declaration("key", sensitive=True)]
    )

    assert projected == {
        "MEIVA_MODEL": ("cluster-default", False),
        "KEY": ("cluster-default", True),
    }


# meiva:note
# A sensitivity flag that is not a real boolean cannot be trusted to redact, so the projection
# refuses it instead of coercing "false" or 0 into a value that would be recorded.
@pytest.mark.parametrize("sensitive", [None, "false", 0])
def test_a_sensitivity_flag_that_is_not_a_boolean_is_refused(
    sensitive: int | str | None,
) -> None:
    with pytest.raises(TypeError, match="boolean sensitive"):
        alias_metadata([_declaration("llm_model", sensitive=sensitive)])


# meiva:note
# One alias cannot carry two policies.
def test_two_fields_declaring_one_alias_are_refused() -> None:
    declarations = [
        _declaration("llm_model", alias="MEIVA_MODEL"),
        _declaration("gateway_url", alias="MEIVA_MODEL"),
    ]

    with pytest.raises(ValueError, match="duplicate env alias"):
        alias_metadata(declarations)


# meiva:note
# Values survive only for an alias registered as not sensitive; a sensitive alias and an
# unregistered one keep their name and drop old and new.
def test_only_registered_non_sensitive_values_survive_redaction() -> None:
    redacted = redact_changes(
        [
            {"alias": "MEIVA_MODEL", "old": "old-model", "new": "test-model"},
            {"alias": "ANTHROPIC_API_KEY", "old": "sk-old", "new": "sk-SECRET-NEW"},
            {"alias": "EXTRA_UNKNOWN_KEY", "old": "x", "new": "y"},
        ],
        _METADATA,
    )

    assert redacted == (
        RedactedChange("MEIVA_MODEL", "cluster-default", False, "old-model", "test-model"),
        RedactedChange("ANTHROPIC_API_KEY", "cluster-default", True, None, None),
        RedactedChange("EXTRA_UNKNOWN_KEY", None, None, None, None),
    )


# meiva:note
# A removal has no new value: the recorded entry carries the old value and a null new one.
def test_a_removal_records_a_null_new_value() -> None:
    (change,) = redact_changes([{"alias": "MEIVA_MODEL", "old": "before", "new": None}], _METADATA)

    assert change.as_record() == {
        "alias": "MEIVA_MODEL",
        "scope": "cluster-default",
        "sensitive": False,
        "old": "before",
        "new": None,
    }


# meiva:note
# A diff entry without an alias is a caller bug and fails loudly rather than being skipped.
def test_a_change_without_an_alias_is_refused() -> None:
    with pytest.raises(KeyError):
        redact_changes([{"old": "x", "new": "y"}], _METADATA)


# meiva:note
# Only the executable survives of a command line: an argument can carry a configuration value.
def test_a_command_line_is_reduced_to_its_executable() -> None:
    name, cmdline = process_label(
        "runner", ["/opt/venv/bin/runner", "--token", "cluster-secret-value"]
    )

    assert name == "runner"
    assert cmdline == "runner [arguments redacted]"
    assert "cluster-secret-value" not in cmdline


# meiva:note
# A process with no command line is labelled by its name, and the label is bounded.
def test_an_empty_command_line_falls_back_to_the_process_name() -> None:
    assert process_label("launchd", []) == ("launchd", "launchd [arguments redacted]")
    assert len(process_label("p", ["x" * 500])[1]) == 200
