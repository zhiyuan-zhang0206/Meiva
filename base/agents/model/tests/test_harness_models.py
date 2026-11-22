from __future__ import annotations

import pytest

from base.agents.model.harness_models import (
    classify_codex_account_probe,
    parse_antigravity_models,
    parse_claude_initialize,
    parse_codex_model_list,
    resolve_harness_catalog_model,
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


def test_parse_claude_initialize_lists_both_the_one_million_selector_and_its_model() -> None:
    result = parse_claude_initialize(
        {
            "models": [
                {"value": "default", "resolvedModel": "claude-sonnet-4-6"},
                {"value": "sonnet[1M]", "resolvedModel": "claude-sonnet-4-6"},
                {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1"},
            ]
        }
    )

    assert [model.id for model in result.models] == [
        "claude-sonnet-4-6",
        "sonnet[1M]",
        "claude-fable-5-1[1m]",
        "claude-fable-5-1",
    ]


# meiva:note
# The Fable rows as Claude Code 2.1.322 and 2.1.304 report them: the CLI lists Fable only through
# its `[1m]` selector, yet `--model claude-fable-5-2` runs, so both IDs must be in the catalog.
@pytest.mark.parametrize(
    "resolved", ["claude-fable-5-2", "claude-fable-5-2[1m]"], ids=["2.1.322", "2.1.304"]
)
def test_parse_claude_initialize_keeps_the_bare_model_behind_a_one_million_selector(
    resolved: str,
) -> None:
    result = parse_claude_initialize(
        {
            "models": [
                {
                    "value": "claude-fable-5-2[1m]",
                    "resolvedModel": resolved,
                    "supportsEffort": True,
                    "supportedEffortLevels": ["low", "medium", "high", "max"],
                },
            ]
        }
    )

    assert result.models == tuple(
        ProbedHarnessModel(
            id=model_id,
            reasoning_effort_options=("low", "medium", "high", "max"),
            reasoning_effort_default="high",
        )
        for model_id in ("claude-fable-5-2[1m]", "claude-fable-5-2")
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
    assert resolve_probed_harness_selection(
        Harness.ANTIGRAVITY, models, requested_model="gpt-oss-120b"
    ) == (
        "gpt-oss-120b",
        "medium",
    )
    assert resolve_probed_harness_selection(
        Harness.CLAUDE_CODE,
        models,
        requested_model="claude-haiku-4-5-20251001",
    ) == ("claude-haiku-4-5-20251001", None)

    with pytest.raises(InvalidModelConfig, match="no model configured"):
        resolve_probed_harness_selection(Harness.ANTIGRAVITY, models)

    with pytest.raises(InvalidModelConfig, match="does not accept reasoning_effort"):
        resolve_probed_harness_selection(
            Harness.CLAUDE_CODE,
            models,
            requested_model="claude-haiku-4-5-20251001",
            requested_effort="low",
        )

    with pytest.raises(InvalidModelConfig, match="not supported by harness"):
        resolve_probed_harness_selection(
            Harness.ANTIGRAVITY,
            models,
            requested_model="gpt-6.1-terra",
        )

    with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
        resolve_probed_harness_selection(
            Harness.ANTIGRAVITY,
            models,
            requested_model="gpt-oss-120b",
            requested_effort="high",
        )


@pytest.mark.parametrize(
    ("raw_model", "expected"),
    [
        ("gemini-4-hydrogen-high", "gemini-4-hydrogen"),
        ("gemini-4-hydrogen-xhigh", "gemini-4-hydrogen"),
        ("gemini-4-carbon-high", "gemini-4-carbon"),
        ("gemini-4-neon-high", "gemini-4-neon"),
        ("gemini-4-neon-xhigh", "gemini-4-neon"),
        ("gemini-4-neon-medium", "gemini-4-neon"),
        ("gemini-4-neon-low", "gemini-4-neon"),
        ("claude-fable-5-2[1m]", "claude-fable-5-2"),
        ("claude-fable-5-2[1M]", "claude-fable-5-2"),
        ("qwen3.8-max", "qwen3.8-max"),
        ("gpt-oss-120b-medium", "gpt-oss-120b"),
        ("gemini-5-foo-minimal", "gemini-5-foo"),
        ("gemini-5-foo-low", "gemini-5-foo"),
        ("gemini-5-foo-medium", "gemini-5-foo"),
        ("gemini-5-foo-high", "gemini-5-foo"),
        ("gemini-5-foo-xhigh", "gemini-5-foo"),
        ("claude-fable-6[1m]", "claude-fable-6"),
        ("claude-fable-6[1M]", "claude-fable-6"),
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("deepseek-flash", "deepseek-flash"),
        ("claude-fable-5-2-fast", "claude-fable-5-2-fast"),
        ("claude-opus-5-5-thinking", "claude-opus-5-5-thinking"),
        ("qwen4-max", "qwen4-max"),
        ("gemini-5-ultra", "gemini-5-ultra"),
        ("gemini-4-neon-max", "gemini-4-neon"),
        ("gemini-4-neon-ultra", "gemini-4-neon"),
    ],
)
def test_resolve_harness_catalog_model_strips_cli_suffixes_without_clobbering_exact_ids(
    raw_model: str, expected: str
) -> None:
    known = {
        "gemini-4-hydrogen",
        "gemini-4-carbon",
        "gemini-4-neon",
        "claude-fable-5-2",
        "qwen3.8",
        "qwen3.8-max",
    }
    assert resolve_harness_catalog_model(raw_model, known) == expected


def test_classify_codex_account_probe_distinguishes_dead_refresh_network_and_live() -> None:
    dead = classify_codex_account_probe(
        {"account": None, "requiresOpenaiAuth": True},
        None,
        None,
        "2026-11-19T07:33:09Z ERROR codex_login::auth::manager: Failed to refresh token: "
        "Your access token could not be refreshed because your refresh token was already used. "
        "Please log out and sign in again.\n",
    )
    assert dead == (
        False,
        "Your access token could not be refreshed because your refresh token was already used. "
        "Please log out and sign in again.",
    )

    rpc_dead = classify_codex_account_probe(
        None,
        "Your access token could not be refreshed. Please log out and sign in again.",
        -32603,
        "",
    )
    assert rpc_dead[0] is False

    offline = classify_codex_account_probe(
        {"account": None, "requiresOpenaiAuth": True},
        None,
        None,
        "ERROR codex_login::auth::manager: Failed to refresh token: dns error",
    )
    assert offline[0] is None

    unsupported = classify_codex_account_probe(None, "Method not found", -32601, "")
    assert unsupported == (None, None)

    assert classify_codex_account_probe(
        {"account": {"type": "chatgpt", "email": "u@example.com"}, "requiresOpenaiAuth": True},
        None,
        None,
        "",
    ) == (True, None)
    assert classify_codex_account_probe(
        {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}, None, None, ""
    ) == (True, None)
