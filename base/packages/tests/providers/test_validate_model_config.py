from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.packages.tests.providers.model_config_process import provider_keys, run_case


class TestValidateModelConfig:
    def test_model_from_config_wins_over_default(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_model_from_config_wins_over_default", provider_keys())

    def test_fallback_to_cluster_default_when_config_omits_model(self, tmp_path: Path) -> None:
        run_case(
            tmp_path,
            "validate_fallback_to_cluster_default_when_config_omits_model",
            provider_keys(),
        )

    def test_fallback_to_cluster_default_when_config_is_none(self, tmp_path: Path) -> None:
        run_case(
            tmp_path, "validate_fallback_to_cluster_default_when_config_is_none", provider_keys()
        )

    def test_no_model_configured_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_no_model_configured_raises", {})

    def test_no_model_configured_empty_config(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_no_model_configured_empty_config", {})

    def test_unknown_model_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_unknown_model_raises", {})

    def test_known_model_succeeds(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_known_model_succeeds", provider_keys())

    def test_all_supported_models_pass_name_check(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_all_supported_models_pass_name_check", provider_keys())

    def test_superseded_model_stays_spawn_valid(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_superseded_model_stays_spawn_valid", provider_keys())

    def test_config_only_validation_keeps_exact_model_effort_checks(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_config_only_validation_keeps_exact_model_effort_checks", {})

    def test_missing_claude_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_claude_key_raises", {})

    def test_missing_deepseek_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_deepseek_key_raises", {})

    def test_missing_gemini_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_gemini_key_raises", {})

    def test_missing_openai_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_openai_key_raises", {})

    def test_missing_mimo_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_mimo_key_raises", {})

    def test_missing_kimi_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_kimi_key_raises", {})

    def test_missing_glm_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_glm_key_raises", {})

    def test_missing_qwen_key_raises(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_missing_qwen_key_raises", {})

    def test_key_present_succeeds(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_key_present_succeeds", {"ANTHROPIC_API_KEY": "sk-ant-123"})

    def test_config_model_with_missing_key_raises(self, tmp_path: Path) -> None:
        run_case(
            tmp_path,
            "validate_config_model_with_missing_key_raises",
            {"DEEPSEEK_API_KEY": "sk-test", "GEMINI_API_KEY": "sk-test"},
        )

    def test_config_model_with_key_present_succeeds(self, tmp_path: Path) -> None:
        run_case(
            tmp_path,
            "validate_config_model_with_key_present_succeeds",
            {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )

    def test_config_with_non_string_model_ignores(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_config_with_non_string_model_ignores", provider_keys())

    def test_override_skips_api_key_check(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_override_skips_api_key_check", {})

    @pytest.mark.parametrize(
        ("model", "effort"),
        [("mimo-v2.6-pro", "none"), ("mimo-v2.6-pro", "high"), ("deepseek-flash", "max")],
    )
    def test_supported_explicit_effort_passes_unchanged(
        self, tmp_path: Path, model: str, effort: str
    ) -> None:
        run_case(
            tmp_path,
            "validate_supported_explicit_effort_passes_unchanged",
            provider_keys(),
            model,
            json.dumps(effort),
        )

    @pytest.mark.parametrize(
        ("model", "effort", "message"),
        [
            ("mimo-v2.6-pro", "low", "unsupported reasoning effort"),
            ("deepseek-flash", "medium", "unsupported reasoning effort"),
            ("gpt-6.1-sol-fast", "none", "unsupported reasoning effort"),
            ("deepseek-flash", "invented", "unknown reasoning effort"),
            ("deepseek-flash", 42, "reasoning_effort must be a string"),
        ],
    )
    def test_invalid_explicit_effort_rejected_even_with_override(
        self, tmp_path: Path, model: str, effort: object, message: str
    ) -> None:
        run_case(
            tmp_path,
            "validate_invalid_explicit_effort_rejected_even_with_override",
            {},
            model,
            json.dumps(effort),
            message,
        )

    def test_restored_model_validates_to_itself(self, tmp_path: Path) -> None:
        run_case(tmp_path, "validate_restored_model_validates_to_itself", provider_keys())
