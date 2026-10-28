# meiva:note
# The chat models OpenAI serves: ``provider.py`` imports ``MODELS`` into the contribution it
# declares, and the pricing gate reads these literal rows without executing the plugin.

from __future__ import annotations

from base.api_contracts.agent_contract import Harness
from base.lm.effort import HarnessModelSpec
from base.lm.registry import ModelSpec, ModelTuning

# meiva:note
# Effort vocabulary shared by GPT-5.6 and GPT-6 Sol/Luna; Astra differs.
EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")

# meiva:note
# Codex app-server model/list publishes these execution efforts independently of API tuning.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
CODEX_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CODEX_ULTRA_EFFORT_LEVELS = (*CODEX_EFFORT_LEVELS, "ultra")

MODELS = {
    "gpt-6-astra": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_ULTRA_EFFORT_LEVELS, "medium")},
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-04",
        # meiva:note
        # GPT-6 Astra rejects "none" and "minimal" (live-checked
        # 2026-09-25), unlike Sol and Luna which accept "none";
        # the builder rejects efforts outside these rungs.
        effort_levels=("low", "medium", "high", "xhigh", "max"),
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
    "gpt-5.6-sol": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_ULTRA_EFFORT_LEVELS, "low")},
        spawnable=True,
        # meiva:note
        # Display-superseded only; existing configs remain valid (2026-09-25).
        superseded_by="gpt-6-sol",
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        # meiva:note
        # Flagship tier (explicit id; the bare gpt-5.6 alias also routes here,
        # but the catalog pins the explicit tier id like terra/luna).
        effort_levels=EFFORT_LEVELS,
        tuning=ModelTuning(
            # meiva:note
            # Pinned 2026-08-01 (task #568): OpenAI documents `medium` as the
            # default reasoning effort (docs/decisions/2026-07-25-per-model-
            # tuning-values.md Decision 4).
            reasoning_effort="medium",
        ),
        media_types=frozenset({"image"}),
    ),
    "gpt-5.6-terra": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_ULTRA_EFFORT_LEVELS, "medium")},
        spawnable=True,
        superseded_by="gpt-6.1-terra",
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        effort_levels=EFFORT_LEVELS,
        # meiva:note
        # Same window, same effort ladder across all three tiers — OpenAI
        # documents no per-tier difference in anything Meiva tunes.
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
    "gpt-5.6-luna": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_EFFORT_LEVELS, "medium")},
        spawnable=True,
        # meiva:note
        # Display-superseded only; existing configs remain valid (2026-09-25).
        superseded_by="gpt-6-luna",
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        effort_levels=EFFORT_LEVELS,
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
    "gpt-6-sol": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_ULTRA_EFFORT_LEVELS, "medium")},
        spawnable=True,
        # meiva:note
        # Display-only supersession: existing agent configs remain valid.
        superseded_by="gpt-6.1-sol",
        context_window=1_050_000,
        knowledge_cutoff="2026-04",
        effort_levels=EFFORT_LEVELS,
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
    "gpt-6.1-sol": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_EFFORT_LEVELS, "medium")},
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-04",
        # meiva:note
        # Live-checked 2026-09-30: none returns 400; low is accepted.
        effort_levels=("low", "medium", "high", "xhigh", "max"),
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
    "gpt-6.1-terra": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_EFFORT_LEVELS, "medium")},
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-06",
        effort_levels=EFFORT_LEVELS,
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
    "gpt-6-luna": ModelSpec(
        provider="gpt",
        external_harnesses={Harness.CODEX: HarnessModelSpec(CODEX_EFFORT_LEVELS, "medium")},
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-05",
        effort_levels=EFFORT_LEVELS,
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image"}),
    ),
}
