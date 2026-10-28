# meiva:note
# The chat models Anthropic serves: ``provider.py`` imports ``MODELS`` into the contribution it
# declares, and the pricing gate reads these literal rows without executing the plugin.

from __future__ import annotations

from base.api_contracts.agent_contract import Harness
from base.lm.effort import HarnessModelSpec
from base.lm.registry import ModelSpec, ModelTuning

# meiva:note
# Effort vocabulary shared by adaptive-thinking Claude models. The per-model declarations remain
# authoritative because the dated haiku-4-5 diverges.
ADAPTIVE_EFFORT = ("low", "medium", "high", "xhigh", "max")

MODELS = {
    "claude-sonnet-5": ModelSpec(
        provider="claude",
        external_harnesses={Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "high")},
        spawnable=True,
        # meiva:note
        # Display-only supersession: existing agent configs remain valid.
        superseded_by="claude-sonnet-5-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # meiva:note
            # Pinned 2026-08-01 (user decision, task #568): the picker must
            # show a concrete default, and Anthropic documents `high` as this
            # family's default effort (docs/decisions/2026-07-25-per-model-
            # tuning-values.md Decision 4; "" is exactly equivalent to omitting
            # the parameter, whose default is high). NOT the ladder floor.
            reasoning_effort="high",
            # meiva:note
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-sonnet-5-5": ModelSpec(
        provider="claude",
        external_harnesses={
            Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "high"),
            Harness.ANTIGRAVITY: HarnessModelSpec(
                ("low", "medium", "high"),
                "high",
                {
                    "low": "claude-sonnet-5-5-low",
                    "medium": "claude-sonnet-5-5-medium",
                    "high": "claude-sonnet-5-5-high",
                },
            ),
        },
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-06",
        effort_levels=ADAPTIVE_EFFORT,
        # meiva:note
        # Live-checked 2026-09-30: thinking.type.disabled 400s.
        thinking_always_on=True,
        tuning=ModelTuning(
            reasoning_effort="high",
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-haiku-5-5": ModelSpec(
        provider="claude",
        external_harnesses={Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "medium")},
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-06",
        effort_levels=ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            reasoning_effort="medium",
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-haiku-4-5-20251001": ModelSpec(
        provider="claude",
        external_harnesses={},
        spawnable=True,
        superseded_by="claude-haiku-5-5",
        context_window=200_000,
        max_output_tokens=64_000,
        knowledge_cutoff="2025-10",
        # meiva:note
        # No wire `effort` field (server 400) — the model publishes
        # onto the manual-thinking on/off binary instead.
        effort_levels=("none", "high"),
        extended_thinking_only=True,
        tuning=ModelTuning(
            # meiva:note
            # Manual extended thinking defaults OFF on this model (budget_tokens
            # default 0) — the honest concrete default is the off rung.
            reasoning_effort="none",
            # meiva:note
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-opus-5": ModelSpec(
        provider="claude",
        external_harnesses={Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "high")},
        spawnable=True,
        # meiva:note
        # Anthropic lists Opus 5 as Legacy and provides a 5.5 migration guide;
        # display only, config remains valid.
        # https://www.anthropic.com/pricing (checked 2026-09-25).
        superseded_by="claude-opus-5-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # meiva:note
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # meiva:note
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-opus-5-5": ModelSpec(
        provider="claude",
        external_harnesses={
            Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "medium"),
            Harness.ANTIGRAVITY: HarnessModelSpec(
                ("low", "medium", "high"),
                "medium",
                {
                    "low": "claude-opus-5-5-low",
                    "medium": "claude-opus-5-5-medium",
                    "high": "claude-opus-5-5-high",
                },
            ),
        },
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-06",
        effort_levels=ADAPTIVE_EFFORT,
        # meiva:note
        # Live-checked 2026-09-25: thinking.type.disabled 400s; adaptive is always on.
        # https://docs.claude.com/en/docs/models/opus-5-5/overview
        thinking_always_on=True,
        tuning=ModelTuning(
            # meiva:note
            # Anthropic documents `medium` as Opus 5.5's default reasoning effort.
            reasoning_effort="medium",
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-fable-5": ModelSpec(
        provider="claude",
        external_harnesses={Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "high")},
        spawnable=True,
        superseded_by="claude-fable-5-1",
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=ADAPTIVE_EFFORT,
        # meiva:note
        # Live-checked 2026-09-25: thinking.type.disabled 400s; adaptive is always on.
        thinking_always_on=True,
        tuning=ModelTuning(
            # meiva:note
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # meiva:note
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
            # meiva:note
            # Fable 5.x shares one combined rate pool across Fable 5.1 and 5:
            # 25-40% of every other model's ITPM/OTPM at every tier, plus 2.5x
            # fewer RPM above the Start tier, while costing 2x Opus and running
            # the longest turns. More 429s means more provider-SDK internal
            # retries, which are silent on the wire.
            llm_retry_max_attempts=10,
            # meiva:note
            # That same silence is what TTFT measures: while the SDK retries a
            # 429 internally the socket produces no event at all, so a throttled
            # (but healthy) request looks identical to a hung one at 30s.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-fable-5-1": ModelSpec(
        provider="claude",
        external_harnesses={Harness.CLAUDE_CODE: HarnessModelSpec(ADAPTIVE_EFFORT, "high")},
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-06",
        effort_levels=ADAPTIVE_EFFORT,
        # meiva:note
        # Live-checked 2026-09-25: thinking.type.disabled 400s; adaptive is always on.
        thinking_always_on=True,
        tuning=ModelTuning(
            # meiva:note
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # meiva:note
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
            # meiva:note
            # Fable 5.x shares one combined rate pool across Fable 5.1 and 5:
            # 25-40% of every other model's ITPM/OTPM at every tier, plus 2.5x
            # fewer RPM above the Start tier, while costing 2x Opus and running
            # the longest turns. More 429s means more provider-SDK internal
            # retries, which are silent on the wire.
            llm_retry_max_attempts=10,
            # meiva:note
            # That same silence is what TTFT measures: while the SDK retries a
            # 429 internally the socket produces no event at all, so a throttled
            # (but healthy) request looks identical to a hung one at 30s.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
}
