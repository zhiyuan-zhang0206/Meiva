"""The gateway root's live inputs for rendering and paging stored history."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from base.agents.history.timeline_inputs import TimelineReadInputs

__all__ = ["TimelineReadPolicy", "UnderstandingReadPolicy"]


@dataclass(frozen=True)
class TimelineReadPolicy:
    """Retain readers without resolving a window, history depth or clock."""

    rendering: TimelineReadInputs
    default_limit: Callable[[], int]
    compact_history: Callable[[], int]
    list_boundary_ids: Callable[..., list[str]] | None = None
    load_segment: Callable[..., list[Any]] | None = None
    load_messages: Callable[..., list[Any]] | None = None
    load_message_count: Callable[..., int] | None = None
    render_items: Callable[..., tuple[list[Any], int]] | None = None


@dataclass(frozen=True)
class UnderstandingReadPolicy:
    """Live readers owned by one gateway lifespan, evaluated at each operation."""

    rendering: TimelineReadInputs
    hierarchy_model: Callable[[], str]
    default_model: Callable[[], str]
    chunk_ratio: Callable[[], float]
    enabled: Callable[[], bool]
