# meiva:note
# What one history request reads from configuration, as values.

from collections.abc import Mapping
from dataclasses import dataclass

from base.agents.history.timeline_inputs import TimelineReadInputs

__all__ = [
    "TimelineRead",
    "UnderstandingRead",
    "understanding_chunk_ratio",
    "understanding_enabled",
]


# meiva:note
# The window size, the compact-history depth and the renderer inputs of one timeline read.
@dataclass(frozen=True)
class TimelineRead:
    rendering: TimelineReadInputs
    default_limit: int
    compact_history: int


# meiva:note
# The models, chunk ratio and feature switch of one understanding read.
@dataclass(frozen=True)
class UnderstandingRead:
    rendering: TimelineReadInputs
    hierarchy_model: str
    default_model: str
    chunk_ratio: float
    enabled: bool


# meiva:note
# The understanding switch of a process.
#
# A process that builds the agent config domain holds the value (`domain_value`). One that
# does not (the gateway profile) reads the cluster's value from its unit `.env`, and the field
# default when the file does not set it.
def understanding_enabled(
    *, domain_value: bool | None, env_aliases: Mapping[str, str], field_default: bool
) -> bool:
    if domain_value is not None:
        return domain_value
    raw = env_aliases.get("MEIVA_UNDERSTANDING_ENABLED")
    if raw is None:
        return field_default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# meiva:note
# The understanding chunk ratio of a process, resolved like `understanding_enabled`.
def understanding_chunk_ratio(
    *, domain_value: float | None, env_aliases: Mapping[str, str], field_default: float
) -> float:
    if domain_value is not None:
        return domain_value
    raw = env_aliases.get("MEIVA_UNDERSTANDING_CHUNK_RATIO")
    return float(raw) if raw else field_default
