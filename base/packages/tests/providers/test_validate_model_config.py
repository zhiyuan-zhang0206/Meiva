from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from base.host.proc import run_bounded


# meiva:link tests/docs/test-fixtures/catalog-boot.meiva.okf.md
def _roster_models() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="meiva-model-roster-") as home:
        result = run_bounded(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_model_config_cases.py")),
                "catalog_roster",
            ],
            timeout=60,
            capture_output=True,
            text=True,
            env={"MEIVA_HOME": home, "MEIVA_CONFIG_FETCH": "skip"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    return TypeAdapter(list[str]).validate_json(result.stdout)


def _provider_keys() -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "sk-test",
        "DEEPSEEK_API_KEY": "sk-test",
        "GEMINI_API_KEY": "sk-test",
        "OPENAI_API_KEY": "sk-test",
        "GLM_API_KEY": "sk-test",
        "DASHSCOPE_API_KEY": "sk-test",
        "MOONSHOT_API_KEY": "sk-test",
        "MIMO_API_KEY": "sk-test",
    }


def _run_case(home: Path, case: str, keys: dict[str, str], *arguments: str) -> None:
    legacy = case.endswith(("missing_mimo_key_raises", "missing_kimi_key_raises"))
    initial = (
        {"MIMO_API_KEY": "legacy-settings-key", "MOONSHOT_API_KEY": "legacy-settings-key"}
        if legacy
        else keys
        if case.startswith("thinking_")
        else {}
    )
    (home / ".env").write_text("".join(f"{name}={value}\n" for (name, value) in initial.items()))
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_model_config_cases.py")),
            case,
            json.dumps(keys),
            *arguments,
        ],
        timeout=60,
        capture_output=True,
        text=True,
        env={
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent"
            if legacy or case.startswith("thinking_")
            else "gateway",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


class TestValidateModelConfig:
    def test_model_from_config_wins_over_default(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_model_from_config_wins_over_default", _provider_keys())

    def test_fallback_to_cluster_default_when_config_omits_model(self, tmp_path: Path) -> None:
        _run_case(
            tmp_path,
            "validate_fallback_to_cluster_default_when_config_omits_model",
            _provider_keys(),
        )

    def test_fallback_to_cluster_default_when_config_is_none(self, tmp_path: Path) -> None:
        _run_case(
            tmp_path, "validate_fallback_to_cluster_default_when_config_is_none", _provider_keys()
        )

    def test_no_model_configured_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_no_model_configured_raises", {})

    def test_no_model_configured_empty_config(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_no_model_configured_empty_config", {})

    def test_unknown_model_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_unknown_model_raises", {})

    def test_known_model_succeeds(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_known_model_succeeds", _provider_keys())

    def test_all_supported_models_pass_name_check(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_all_supported_models_pass_name_check", _provider_keys())

    def test_superseded_model_stays_spawn_valid(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_superseded_model_stays_spawn_valid", _provider_keys())

    def test_config_only_validation_keeps_exact_model_effort_checks(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_config_only_validation_keeps_exact_model_effort_checks", {})

    def test_missing_claude_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_claude_key_raises", {})

    def test_missing_deepseek_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_deepseek_key_raises", {})

    def test_missing_gemini_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_gemini_key_raises", {})

    def test_missing_openai_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_openai_key_raises", {})

    def test_missing_mimo_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_mimo_key_raises", {})

    def test_missing_kimi_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_kimi_key_raises", {})

    def test_missing_glm_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_glm_key_raises", {})

    def test_missing_qwen_key_raises(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_missing_qwen_key_raises", {})

    def test_key_present_succeeds(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_key_present_succeeds", {"ANTHROPIC_API_KEY": "sk-ant-123"})

    def test_config_model_with_missing_key_raises(self, tmp_path: Path) -> None:
        _run_case(
            tmp_path,
            "validate_config_model_with_missing_key_raises",
            {"DEEPSEEK_API_KEY": "sk-test", "GEMINI_API_KEY": "sk-test"},
        )

    def test_config_model_with_key_present_succeeds(self, tmp_path: Path) -> None:
        _run_case(
            tmp_path,
            "validate_config_model_with_key_present_succeeds",
            {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )

    def test_config_with_non_string_model_ignores(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_config_with_non_string_model_ignores", _provider_keys())

    def test_override_skips_api_key_check(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_override_skips_api_key_check", {})

    @pytest.mark.parametrize(
        ("model", "effort"),
        [("mimo-v2.6-pro", "none"), ("mimo-v2.6-pro", "high"), ("deepseek-flash", "max")],
    )
    def test_supported_explicit_effort_passes_unchanged(
        self, tmp_path: Path, model: str, effort: str
    ) -> None:
        _run_case(
            tmp_path,
            "validate_supported_explicit_effort_passes_unchanged",
            _provider_keys(),
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
        _run_case(
            tmp_path,
            "validate_invalid_explicit_effort_rejected_even_with_override",
            {},
            model,
            json.dumps(effort),
            message,
        )

    def test_restored_model_validates_to_itself(self, tmp_path: Path) -> None:
        _run_case(tmp_path, "validate_restored_model_validates_to_itself", _provider_keys())


class TestThinkingDisabledAcrossRoster:
    @pytest.mark.parametrize(
        "model",
        _roster_models(),
    )
    def test_roster_model_constructs_with_thinking_disabled(
        self, tmp_path: Path, model: str
    ) -> None:
        _run_case(
            tmp_path,
            "thinking_roster_model_constructs_with_thinking_disabled",
            _provider_keys(),
            model,
        )

    def test_unregistered_gemini_thinking_disabled_is_noop(self, tmp_path: Path) -> None:
        _run_case(
            tmp_path,
            "thinking_unregistered_gemini_thinking_disabled_is_noop",
            {"GEMINI_API_KEY": "sk-test"},
        )

    def test_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level(
        self, tmp_path: Path
    ) -> None:
        _run_case(
            tmp_path,
            "thinking_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level",
            {"GEMINI_API_KEY": "sk-test"},
        )
