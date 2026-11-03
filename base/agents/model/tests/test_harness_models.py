from __future__ import annotations

import pytest

from base.agents.model.harness_models import (
    parse_antigravity_models,
    parse_claude_initialize,
    parse_codex_model_list,
    resolve_probed_harness_selection,
)
from base.api_contracts.agent_contract import (
    Harness,
    InvalidModelConfig,
    ProbedHarnessModel,
)
from base.lm.harness.antigravity import resolve_antigravity_cli_model


def test_parse_codex_model_list_extracts_live_models_efforts_and_defaults() -> None:
    page_1 = {
        "data": [
            {"id": "hidden-internal", "hidden": True, "supportedReasoningEfforts": ["low"]},
            {
                "id": "gpt-6.1-sol",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "xhigh"},
                ],
            },
            {
                "id": "gpt-6.1-terra",
                "isDefault": True,
                "defaultReasoningEffort": "low",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "ultra"},
                ],
            },
        ]
    }
    page_2 = {"data": [{"id": "gpt-6.1-terra", "supportedReasoningEfforts": ["low"]}]}
    result = parse_codex_model_list([page_1, page_2])
    assert result.error is None
    assert result.default_model == "gpt-6.1-terra"
    assert result.models == (
        ProbedHarnessModel(
            id="gpt-6.1-sol",
            reasoning_effort_options=("low", "medium", "high", "xhigh"),
            reasoning_effort_default="medium",
        ),
        ProbedHarnessModel(
            id="gpt-6.1-terra",
            reasoning_effort_options=("low", "medium", "high", "ultra"),
            reasoning_effort_default="low",
        ),
    )


def test_parse_claude_initialize_keeps_uncatalogued_and_effortless_models() -> None:
    payload = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "response": {
                "models": [
                    {
                        "value": "default",
                        "resolvedModel": "claude-sonnet-5-5",
                        "supportsEffort": True,
                        "supportedEffortLevels": ["low", "medium", "high", "max"],
                        "defaultEffort": "high",
                    },
                    {
                        "value": "opus",
                        "resolvedModel": "claude-opus-4-6",
                        "supportsEffort": True,
                        "supportedEffortLevels": ["low", "medium", "high"],
                    },
                    {
                        "value": "haiku",
                        "resolvedModel": "claude-haiku-4-5-20251001",
                        "supportsEffort": False,
                    },
                ]
            },
        },
    }
    result = parse_claude_initialize(payload)
    assert result.error is None
    assert result.default_model == "claude-sonnet-5-5"
    assert result.models == (
        ProbedHarnessModel(
            id="claude-sonnet-5-5",
            reasoning_effort_options=("low", "medium", "high", "max"),
            reasoning_effort_default="high",
        ),
        ProbedHarnessModel(
            id="claude-opus-4-6",
            reasoning_effort_options=("low", "medium", "high"),
            reasoning_effort_default="high",
        ),
        ProbedHarnessModel(id="claude-haiku-4-5-20251001"),
    )


def test_parse_antigravity_models_groups_effort_suffixes_and_uncatalogued_rows() -> None:
    stdout = "\n".join(
        [
            "\x1b[2K\rFetching available models...",
            "gemini-4-argon-high\tGemini 4 Argon (High)",
            "gemini-4-argon-low\tGemini 4 Argon (Low)",
            "gpt-oss-120b-medium  GPT OSS 120B (Medium)",
            "claude-opus-5-5-thinking\tClaude Opus 5.5 Thinking",
        ]
    )
    result = parse_antigravity_models(stdout)
    assert result.error is None
    assert result.default_model == "gemini-4-argon"
    assert result.models == (
        ProbedHarnessModel(
            id="gemini-4-argon",
            reasoning_effort_options=("low", "high"),
            reasoning_effort_default="high",
        ),
        ProbedHarnessModel(
            id="gpt-oss-120b",
            reasoning_effort_options=("medium",),
            reasoning_effort_default="medium",
        ),
        ProbedHarnessModel(id="claude-opus-5-5-thinking"),
    )
    assert resolve_antigravity_cli_model("gpt-oss-120b", "medium") == "gpt-oss-120b-medium"
    assert (
        resolve_antigravity_cli_model("claude-opus-5-5-thinking", None)
        == "claude-opus-5-5-thinking"
    )
    assert resolve_antigravity_cli_model("gemini-4-argon-high", "high") == "gemini-4-argon-high"


def test_resolve_probed_harness_selection_enforces_live_models_and_efforts() -> None:
    models = (
        ProbedHarnessModel(
            id="gpt-oss-120b",
            reasoning_effort_options=("medium",),
            reasoning_effort_default="medium",
        ),
        ProbedHarnessModel(id="claude-haiku-4-5-20251001"),
    )
    assert resolve_probed_harness_selection(Harness.ANTIGRAVITY, models, "gpt-oss-120b") == (
        "gpt-oss-120b",
        "medium",
    )
    assert resolve_probed_harness_selection(
        Harness.CLAUDE_CODE,
        models,
        "gpt-oss-120b",
        requested_model="claude-haiku-4-5-20251001",
    ) == ("claude-haiku-4-5-20251001", None)

    with pytest.raises(InvalidModelConfig, match="does not accept reasoning_effort"):
        resolve_probed_harness_selection(
            Harness.CLAUDE_CODE,
            models,
            "gpt-oss-120b",
            requested_model="claude-haiku-4-5-20251001",
            requested_effort="low",
        )

    with pytest.raises(InvalidModelConfig, match="not supported by harness"):
        resolve_probed_harness_selection(
            Harness.ANTIGRAVITY,
            models,
            "gpt-oss-120b",
            requested_model="gpt-6.1-terra",
        )

    with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
        resolve_probed_harness_selection(
            Harness.ANTIGRAVITY,
            models,
            "gpt-oss-120b",
            requested_model="gpt-oss-120b",
            requested_effort="high",
        )
