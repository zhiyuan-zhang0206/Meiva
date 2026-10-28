"""Reasoning-effort vocabulary and exact per-model validation.

Split out of `base/lm/factory.py` (its companion module — factory re-exports
these names, so callers and tests keep importing from factory). Provider plugins
own endpoint vocabularies and wire switches. This module keeps only the shared
``MEIVA_REASONING_EFFORT`` vocabulary, its public SDK enum/coercion surface, and
validation against a plugin's declared levels without remapping the selection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# meiva:note
# Raw per-agent effort settings include Gemini's minimal and Codex's ultra alongside the public
# ReasoningEffort enum. Each native model or external harness binding validates its own ladder;
# adding a harness-only rung does not enable it on native API models.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
EFFORT_VOCAB: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


class ReasoningEffort(StrEnum):
    """Reasoning-effort levels, ordered from weakest to strongest.

    A member and its string value are interchangeable."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


def coerce_effort(effort: str | ReasoningEffort | None, *, example: str) -> ReasoningEffort | None:
    """Normalize the public `effort` knob onto the enum; None passes through.

    The one effort contract for the SDK batch entry points (`meiva.web.fetch` /
    `meiva.understand`): a `ReasoningEffort` member or its literal string value
    normalizes to the enum; None (fetch's "keep the settings default") passes
    through. `example` names the calling API in error messages.

    Raises:
        TypeError: `effort` is neither a string nor a `ReasoningEffort` member.
        ValueError: a string outside the public vocabulary. `minimal` is
            gemini-internal (a `thinking_level`, not a cross-provider effort
            level) and is rejected here — it is not a `ReasoningEffort` member.
    """
    if effort is None:
        return None
    if not isinstance(effort, (str, ReasoningEffort)):
        raise TypeError(
            f"effort must be a ReasoningEffort member or one of "
            f"{'/'.join(e.value for e in ReasoningEffort)}, got {type(effort).__name__}. "
            f"Example: {example}"
        )
    try:
        return ReasoningEffort(effort)
    except ValueError:
        raise ValueError(
            f"unknown effort {effort!r} — expected one of "
            f"{'/'.join(e.value for e in ReasoningEffort)}. "
            f"Example: {example}"
        ) from None


def validate_effort(effort: str, allowed: tuple[str, ...], *, target: str) -> str:
    """Accept an exact model-supported effort value or reject it before dispatch."""
    if effort in allowed:
        return effort
    if effort not in EFFORT_VOCAB:
        raise ValueError(
            f"unknown reasoning effort {effort!r} — expected one of {'/'.join(EFFORT_VOCAB)}"
        )
    raise ValueError(
        f"unsupported reasoning effort {effort!r} for {target} — "
        f"expected one of {'/'.join(allowed)} (or empty for the model default)"
    )


@dataclass(frozen=True)
class HarnessModelSpec:
    effort_levels: tuple[str, ...]
    default_effort: str
    model_by_effort: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        if not self.effort_levels or self.default_effort not in self.effort_levels:
            raise ValueError("external harness effort default must belong to its nonempty ladder")
        if self.model_by_effort and (
            set(self.model_by_effort) != set(self.effort_levels)
            or not all(self.model_by_effort.values())
        ):
            raise ValueError("external harness CLI model mapping must cover every effort")

    def selection(self, model: str, effort: str | None) -> tuple[str, str]:
        selected = effort or self.default_effort
        if selected not in self.effort_levels:
            raise ValueError(
                f"unsupported reasoning effort {selected!r} for external model {model!r}; "
                f"expected one of {self.effort_levels}"
            )
        return self.model_by_effort.get(selected, model), selected
