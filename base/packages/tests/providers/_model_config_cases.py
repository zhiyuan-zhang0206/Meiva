from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from io import StringIO

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger
from pydantic import TypeAdapter

from base.config import settings
from base.host.env.agent_slices import ModelOverrides
from base.host.env.dotenv_boot import enforce_cluster_env_authority
from base.lm.catalog import ModelCatalog
from base.lm.factory import (
    build_chat_model,
    process_provider_keys,
    validate_model_config,
    validate_model_launch,
)
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import inert_keys
from base.paths.home import resolve_meiva_home


def validate_model_from_config_wins_over_default(
    model_catalog: ModelCatalog,
) -> None:
    result = validate_model_launch(
        model="claude-sonnet-5",
        config={"llm_model": "deepseek-flash"},
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "deepseek-flash"


def validate_fallback_to_cluster_default_when_config_omits_model(
    model_catalog: ModelCatalog,
) -> None:
    result = validate_model_launch(
        model="deepseek-flash",
        config={"some_other_key": "value"},
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "deepseek-flash"


def validate_fallback_to_cluster_default_when_config_is_none(
    model_catalog: ModelCatalog,
) -> None:
    result = validate_model_launch(
        model="deepseek-flash",
        config=None,
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "deepseek-flash"


def validate_no_model_configured_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="no model configured"):
        validate_model_launch(
            model=None,
            config=None,
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_no_model_configured_empty_config(
    model_catalog: ModelCatalog,
) -> None:
    with pytest.raises(ValueError, match="no model configured"):
        validate_model_launch(
            model=None,
            config={},
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_unknown_model_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="unknown model 'not-a-real-model'"):
        validate_model_launch(
            model="not-a-real-model",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_known_model_succeeds(model_catalog: ModelCatalog) -> None:
    result = validate_model_launch(
        model="deepseek-flash",
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "deepseek-flash"


def validate_all_supported_models_pass_name_check(
    model_catalog: ModelCatalog,
) -> None:
    all_models = [m for models in model_catalog.supported_models.values() for m in models]
    for m in all_models:
        result = validate_model_launch(
            model=m,
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )
        assert result == m


def validate_superseded_model_stays_spawn_valid(
    model_catalog: ModelCatalog,
) -> None:
    assert (
        validate_model_launch(
            model="kimi-k3",
            config={"llm_model": "kimi-k3"},
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )
        == "kimi-k3"
    )
    model_catalog = replace(
        model_catalog,
        models={
            **model_catalog.models,
            "glm-5.2": replace(model_catalog.models["glm-5.2"], superseded_by="kimi-k3"),
        },
    )
    result = validate_model_launch(
        model="glm-5.2",
        config={"llm_model": "glm-5.2"},
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "glm-5.2"


def validate_config_only_validation_keeps_exact_model_effort_checks(
    model_catalog: ModelCatalog,
) -> None:
    assert (
        validate_model_config(
            config={"llm_model": "deepseek-flash", "reasoning_effort": "max"}, catalog=model_catalog
        )
        == "deepseek-flash"
    )
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        validate_model_config(
            config={"llm_model": "deepseek-flash", "reasoning_effort": "low"}, catalog=model_catalog
        )
    with pytest.raises(ValueError, match="unknown model"):
        validate_model_config(model="unknown-model", catalog=model_catalog)


def validate_missing_claude_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        validate_model_launch(
            model="claude-sonnet-5",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_missing_deepseek_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_launch(
            model="deepseek-flash",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_missing_gemini_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        validate_model_launch(
            model="gemini-3.5-flash",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_missing_openai_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_model_launch(
            model="gpt-5.6-sol",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_missing_mimo_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="MIMO_API_KEY"):
        validate_model_launch(
            model="mimo-v2.6-pro",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_missing_kimi_key_raises(model_catalog: ModelCatalog) -> None:
    for model in ("kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed"):
        with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
            validate_model_launch(
                model=model,
                catalog=model_catalog,
                llm_override="",
                keys=process_provider_keys(model_catalog),
            )


def validate_missing_glm_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        validate_model_launch(
            model="glm-5.2",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_missing_qwen_key_raises(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        validate_model_launch(
            model="qwen3.8-max",
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_key_present_succeeds(model_catalog: ModelCatalog) -> None:
    result = validate_model_launch(
        model="claude-sonnet-5",
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "claude-sonnet-5"


def validate_config_model_with_missing_key_raises(
    model_catalog: ModelCatalog,
) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        validate_model_launch(
            model="deepseek-flash",
            config={"llm_model": "claude-sonnet-5"},
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )


def validate_config_model_with_key_present_succeeds(
    model_catalog: ModelCatalog,
) -> None:
    result = validate_model_launch(
        model="deepseek-flash",
        config={"llm_model": "claude-sonnet-5"},
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "claude-sonnet-5"


def validate_config_with_non_string_model_ignores(
    model_catalog: ModelCatalog,
) -> None:
    result = validate_model_launch(
        model="deepseek-flash",
        config={"llm_model": 42},
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "deepseek-flash"


def validate_override_skips_api_key_check(model_catalog: ModelCatalog) -> None:
    llm_override = "tests.fakes:build"
    result = validate_model_launch(
        model="claude-sonnet-5",
        catalog=model_catalog,
        llm_override=llm_override,
        keys=process_provider_keys(model_catalog),
    )
    assert result == "claude-sonnet-5"


def validate_supported_explicit_effort_passes_unchanged(
    model_catalog: ModelCatalog, model: str, effort: str
) -> None:
    config: dict[str, object] = {"reasoning_effort": effort}
    assert (
        validate_model_launch(
            model=model,
            config=config,
            catalog=model_catalog,
            llm_override="",
            keys=process_provider_keys(model_catalog),
        )
        == model
    )
    assert config["reasoning_effort"] == effort


def validate_invalid_explicit_effort_rejected_even_with_override(
    model_catalog: ModelCatalog, model: str, effort: object, message: str
) -> None:
    llm_override = "tests.fakes:build"
    with pytest.raises(ValueError, match=message):
        validate_model_launch(
            model=model,
            config={"reasoning_effort": effort},
            catalog=model_catalog,
            llm_override=llm_override,
            keys=process_provider_keys(model_catalog),
        )


def validate_restored_model_validates_to_itself(
    model_catalog: ModelCatalog,
) -> None:
    result = validate_model_launch(
        model="gemini-3.8-flash",
        catalog=model_catalog,
        llm_override="",
        keys=process_provider_keys(model_catalog),
    )
    assert result == "gemini-3.8-flash"


def thinking_roster_model_constructs_with_thinking_disabled(
    model_catalog: ModelCatalog, model: str
) -> None:
    llm = build_chat_model(
        model,
        thinking={"type": "disabled"},
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    assert isinstance(llm, BaseChatModel)


def thinking_unregistered_gemini_thinking_disabled_is_noop(
    model_catalog: ModelCatalog, log_output: StringIO
) -> None:
    model = "gemini-2.5-flash"
    assert model not in model_catalog.models
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = build_chat_model(
        model,
        thinking={"type": "disabled"},
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.thinking_level is None
    assert llm.include_thoughts is None
    assert any(model in line and "ignored" in line for line in log_output.getvalue().splitlines())


def thinking_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level(
    model_catalog: ModelCatalog,
) -> None:
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = build_chat_model(
        "gemini-3.1-pro-preview",
        thinking={"type": "disabled"},
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.thinking_level == "low"
    assert llm.include_thoughts is False


def _model_roster(catalog: ModelCatalog) -> list[str]:
    return [model for rows in catalog.supported_models.values() for model in rows]


def main() -> None:
    case = sys.argv[1]
    if case == "catalog_roster":
        catalog = build_model_catalog(home=resolve_meiva_home())
        sys.stdout.write(json.dumps(_model_roster(catalog)) + "\n")
        return
    keys = TypeAdapter(dict[str, str]).validate_json(sys.argv[2])
    home = resolve_meiva_home()
    snapshot = settings.lm
    catalog = build_model_catalog(home=home)
    legacy = case.endswith(("missing_mimo_key_raises", "missing_kimi_key_raises"))
    if legacy:
        assert snapshot.xiaomi_api_key is not None
        assert snapshot.xiaomi_api_key.get_secret_value() == "legacy-settings-key"
        assert snapshot.moonshot_api_key is not None
        assert snapshot.moonshot_api_key.get_secret_value() == "legacy-settings-key"
    (home / ".env").write_text("".join(f"{name}={value}\n" for name, value in keys.items()))
    if legacy:
        enforce_cluster_env_authority(os.environ, home)
        assert "MIMO_API_KEY" not in os.environ
        assert "MOONSHOT_API_KEY" not in os.environ
    if case.startswith("validate_"):
        assert all(name not in os.environ for name in keys)
    output = StringIO()
    sink = logger.add(output, diagnose=False)
    try:
        dispatch(case, catalog, output)
    finally:
        logger.remove(sink)


_SIMPLE_CASES: dict[str, Callable[[ModelCatalog], None]] = {
    "validate_model_from_config_wins_over_default": validate_model_from_config_wins_over_default,
    "validate_fallback_to_cluster_default_when_config_omits_model": validate_fallback_to_cluster_default_when_config_omits_model,
    "validate_fallback_to_cluster_default_when_config_is_none": validate_fallback_to_cluster_default_when_config_is_none,
    "validate_no_model_configured_raises": validate_no_model_configured_raises,
    "validate_no_model_configured_empty_config": validate_no_model_configured_empty_config,
    "validate_unknown_model_raises": validate_unknown_model_raises,
    "validate_known_model_succeeds": validate_known_model_succeeds,
    "validate_all_supported_models_pass_name_check": validate_all_supported_models_pass_name_check,
    "validate_superseded_model_stays_spawn_valid": validate_superseded_model_stays_spawn_valid,
    "validate_config_only_validation_keeps_exact_model_effort_checks": validate_config_only_validation_keeps_exact_model_effort_checks,
    "validate_missing_claude_key_raises": validate_missing_claude_key_raises,
    "validate_missing_deepseek_key_raises": validate_missing_deepseek_key_raises,
    "validate_missing_gemini_key_raises": validate_missing_gemini_key_raises,
    "validate_missing_openai_key_raises": validate_missing_openai_key_raises,
    "validate_missing_mimo_key_raises": validate_missing_mimo_key_raises,
    "validate_missing_kimi_key_raises": validate_missing_kimi_key_raises,
    "validate_missing_glm_key_raises": validate_missing_glm_key_raises,
    "validate_missing_qwen_key_raises": validate_missing_qwen_key_raises,
    "validate_key_present_succeeds": validate_key_present_succeeds,
    "validate_config_model_with_missing_key_raises": validate_config_model_with_missing_key_raises,
    "validate_config_model_with_key_present_succeeds": validate_config_model_with_key_present_succeeds,
    "validate_config_with_non_string_model_ignores": validate_config_with_non_string_model_ignores,
    "validate_override_skips_api_key_check": validate_override_skips_api_key_check,
    "validate_restored_model_validates_to_itself": validate_restored_model_validates_to_itself,
    "thinking_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level": thinking_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level,
}


def dispatch(case: str, catalog: ModelCatalog, output: StringIO) -> None:
    if case == "validate_supported_explicit_effort_passes_unchanged":
        validate_supported_explicit_effort_passes_unchanged(
            catalog, sys.argv[3], TypeAdapter(str).validate_json(sys.argv[4])
        )
    elif case == "validate_invalid_explicit_effort_rejected_even_with_override":
        validate_invalid_explicit_effort_rejected_even_with_override(
            catalog, sys.argv[3], TypeAdapter(object).validate_json(sys.argv[4]), sys.argv[5]
        )
    elif case == "thinking_roster_model_constructs_with_thinking_disabled":
        thinking_roster_model_constructs_with_thinking_disabled(catalog, sys.argv[3])
    elif case == "thinking_unregistered_gemini_thinking_disabled_is_noop":
        thinking_unregistered_gemini_thinking_disabled_is_noop(catalog, output)
    else:
        _SIMPLE_CASES[case](catalog)


if __name__ == "__main__":
    main()
