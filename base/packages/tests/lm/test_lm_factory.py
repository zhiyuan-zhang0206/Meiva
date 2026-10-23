from __future__ import annotations

from dataclasses import replace

import pytest

from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.factory import (
    model_supports_vision,
    provider_key_map,
    validate_model_config,
    validate_model_launch,
)
from base.packages.tests.model_catalog import AddModels


def test_a_key_in_the_held_keys_authorizes_the_model(model_catalog: ModelCatalog) -> None:
    assert (
        validate_model_launch(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            keys=ProviderKeys({"DEEPSEEK_API_KEY": "sk-env"}),
            llm_override="",
        )
        == "deepseek-flash"
    )


def test_a_model_whose_key_is_not_held_fails(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_launch(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            keys=ProviderKeys({"SOME_OTHER_KEY": "x"}),
            llm_override="",
        )


# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
@pytest.mark.parametrize(
    ("prefix", "model", "display", "key_env"),
    (
        ("deepseek-", "deepseek-flash", "DeepSeek", "DEEPSEEK_API_KEY"),
        ("gemini-", "gemini-3.5-flash", "Google", "GEMINI_API_KEY"),
        ("claude-", "claude-sonnet-5", "Anthropic", "ANTHROPIC_API_KEY"),
        ("gpt-", "gpt-5.6-sol", "OpenAI", "OPENAI_API_KEY"),
        ("qwen", "qwen3.8-max", "Alibaba", "DASHSCOPE_API_KEY"),
        ("glm-", "glm-5.2", "Zhipu", "GLM_API_KEY"),
        ("kimi-", "kimi-k3", "Moonshot", "MOONSHOT_API_KEY"),
        ("mimo-", "mimo-v2.6-pro", "Xiaomi", "MIMO_API_KEY"),
    ),
)
def test_a_provider_key_is_only_ever_the_declared_env_name(
    prefix: str, model: str, display: str, key_env: str, model_catalog: ModelCatalog
) -> None:
    legacy_field_name = key_env.lower()
    assert provider_key_map(catalog=model_catalog)[prefix] == (display, key_env)
    with pytest.raises(ValueError, match=key_env):
        validate_model_launch(
            model=model,
            config={},
            catalog=model_catalog,
            keys=ProviderKeys({legacy_field_name: "sk-test"}),
            llm_override="",
        )


def test_withdrawn_model_resolves_to_its_fallback_at_the_spawn_boundary(
    add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    model = "deepseek-retired-fixture"
    model_catalog = add_models(
        model_catalog,
        {
            model: replace(
                model_catalog.models["deepseek-flash"],
                spawnable=False,
                unavailable_fallback="deepseek-flash",
            )
        },
    )
    keys = ProviderKeys({"DEEPSEEK_API_KEY": "sk-file-value"})
    assert (
        validate_model_launch(
            model=model, config={}, catalog=model_catalog, keys=keys, llm_override=""
        )
        == "deepseek-flash"
    )
    assert (
        validate_model_launch(
            model=None,
            config={"llm_model": model},
            catalog=model_catalog,
            keys=keys,
            llm_override="",
        )
        == "deepseek-flash"
    )


@pytest.mark.parametrize(
    "model",
    (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "mimo-v2.5-pro",
        "mimo-v2.5-pro-ultraspeed",
    ),
)
def test_removed_model_fails_spawn_validation(model: str, *, model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match=f"unknown model '{model}'"):
        validate_model_config(model=model, config={}, catalog=model_catalog)
    with pytest.raises(ValueError, match=f"unknown model '{model}'"):
        validate_model_config(model=None, config={"llm_model": model}, catalog=model_catalog)


def test_unknown_model_still_fails(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="unknown model"):
        validate_model_config(model="no-such-model-xyz", config={}, catalog=model_catalog)


class TestModelSupportsVision:
    def test_registered_vision_model_passes(self, *, model_catalog: ModelCatalog) -> None:
        assert model_supports_vision("glm-5.3-flash", catalog=model_catalog) is True

    def test_registered_deepseek_flash_passes(self, *, model_catalog: ModelCatalog) -> None:
        assert model_supports_vision("deepseek-flash", catalog=model_catalog) is True

    def test_registered_text_only_model_fails(self, *, model_catalog: ModelCatalog) -> None:
        assert model_supports_vision("glm-5.3", catalog=model_catalog) is False

    def test_unregistered_id_falls_back_to_prefix(self, *, model_catalog: ModelCatalog) -> None:
        assert model_supports_vision("claude-unknown-id", catalog=model_catalog) is True
        assert model_supports_vision("gemini-4-experiment", catalog=model_catalog) is True
        assert model_supports_vision("gpt-unknown-id", catalog=model_catalog) is True
        assert model_supports_vision("qwen3.8-unknown-id", catalog=model_catalog) is True
        assert model_supports_vision("deepseek-unknown-id", catalog=model_catalog) is False
