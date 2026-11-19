import pytest

from base.config import settings
from base.lm.catalog import ModelCatalog
from base.packages.plugins.config_registration import validate_config_overlay
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from base.paths import meiva_home


def test_the_default_skill_recall_model_is_a_registered_decision_model(
    model_catalog: ModelCatalog,
) -> None:
    assert settings.agent.skill_recall_model in model_catalog.decision_models
    validate_config_overlay(
        {"skill_recall_model": settings.agent.skill_recall_model},
        models=model_catalog.models,
        decision_models=model_catalog.decision_models,
        home=meiva_home(),
    )


def test_a_chat_model_is_not_a_decision_model(model_catalog: ModelCatalog) -> None:
    with pytest.raises(InvalidConfigOverlay, match="not a registered decision model") as exc:
        validate_config_overlay(
            {"skill_recall_model": "deepseek-flash"},
            models=model_catalog.models,
            decision_models=model_catalog.decision_models,
            home=meiva_home(),
        )

    assert "skill_recall_model" in str(exc.value)
    assert settings.agent.skill_recall_model in str(exc.value)


def test_an_unknown_id_is_rejected(model_catalog: ModelCatalog) -> None:
    with pytest.raises(InvalidConfigOverlay, match="not a registered decision model"):
        validate_config_overlay(
            {"skill_recall_model": "jev-0.0"},
            models=model_catalog.models,
            decision_models=model_catalog.decision_models,
            home=meiva_home(),
        )


def test_a_caller_without_the_decision_table_cannot_admit_the_field(
    model_catalog: ModelCatalog,
) -> None:
    with pytest.raises(InvalidConfigOverlay, match="no decision-model table"):
        validate_config_overlay(
            {"skill_recall_model": "jev-1.13.0"}, models=model_catalog.models, home=meiva_home()
        )


def test_an_overlay_without_the_field_needs_no_decision_table(model_catalog: ModelCatalog) -> None:
    validate_config_overlay(
        {"skill_recall_enabled": False}, models=model_catalog.models, home=meiva_home()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_recall_threshold", 0.0),
        ("skill_recall_threshold", 1.5),
        ("skill_recall_threshold", float("nan")),
        ("skill_recall_max", -1),
        ("skill_recall_deadline_seconds", 0.0),
        ("skill_recall_deadline_seconds", float("inf")),
    ],
)
def test_the_numeric_fields_are_range_checked(
    model_catalog: ModelCatalog, field: str, value: float
) -> None:
    with pytest.raises(InvalidConfigOverlay):
        validate_config_overlay(
            {field: value},
            models=model_catalog.models,
            decision_models=model_catalog.decision_models,
            home=meiva_home(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_recall_enabled", False),
        ("skill_recall_threshold", 0.75),
        ("skill_recall_max", 3),
        ("skill_recall_deadline_seconds", 6.0),
    ],
)
def test_sane_values_are_admitted(model_catalog: ModelCatalog, field: str, value: object) -> None:
    validate_config_overlay(
        {field: value},
        models=model_catalog.models,
        decision_models=model_catalog.decision_models,
        home=meiva_home(),
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_fast_acknowledgement_overlay_inherits_or_overrides_strict_booleans(
    model_catalog: ModelCatalog, enabled: bool
) -> None:
    from base.config.agent_pins import frozen_field_names
    from base.config.tests.configuration import defaults
    from base.host.env.agent_slices import AgentSlices
    from base.packages.plugins.config_registration import resolve_overlay_targets

    assert "fast_acknowledgement" not in frozen_field_names()
    assert resolve_overlay_targets({"fast_acknowledgement": enabled}, home=meiva_home()) == {
        "fast_acknowledgement": (None, "fast_acknowledgement")
    }
    validate_config_overlay(
        {"fast_acknowledgement": enabled},
        models=model_catalog.models,
        home=meiva_home(),
    )
    overridden = AgentSlices.resolve(
        {"fast_acknowledgement": enabled},
        default_reader=defaults(agent__fast_acknowledgement=not enabled),
    )
    assert overridden.prompt.fast_acknowledgement is enabled
    inherited = AgentSlices.resolve(
        None, default_reader=defaults(agent__fast_acknowledgement=enabled)
    )
    assert inherited.prompt.fast_acknowledgement is enabled


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0])
def test_fast_acknowledgement_overlay_rejects_coercible_non_booleans(
    model_catalog: ModelCatalog, invalid: object
) -> None:
    with pytest.raises(InvalidConfigOverlay, match="must be a boolean"):
        validate_config_overlay(
            {"fast_acknowledgement": invalid},
            models=model_catalog.models,
            home=meiva_home(),
        )


def test_fast_acknowledgement_overlay_rejects_none(model_catalog: ModelCatalog) -> None:
    with pytest.raises(InvalidConfigOverlay):
        validate_config_overlay(
            {"fast_acknowledgement": None},
            models=model_catalog.models,
            home=meiva_home(),
        )
