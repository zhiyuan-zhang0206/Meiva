"""Independent live state and evidence-bearing persisted agent statistics."""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
)

from base.agents.observation.evidence import AgentObservation
from base.api_contracts.agent_contract import LivenessState
from gateway.schemas.stats import StatsWindowHours
from ops.rpc_schemas import ShellInfo


class AgentCost(BaseModel):
    """Usage-time price and reported tokens for one agent over the requested window."""

    model_config = ConfigDict(frozen=True)

    cost_usd: float = Field(ge=0)
    cost_usd_api: float = Field(
        ge=0, description="API calls valued at their usage-time USD list prices."
    )
    cost_usd_subscription: float = Field(
        ge=0,
        description=(
            "List value the calls a subscription covered would have cost; not money owed."
        ),
    )
    unpriced_calls: NonNegativeInt
    unpriced_calls_api: NonNegativeInt = Field(
        description="Metered calls with no usage-time price snapshot; they contribute 0."
    )
    unpriced_calls_subscription: NonNegativeInt = Field(
        description=(
            "Subscription-covered calls with no usage-time price snapshot; they contribute 0."
        ),
    )
    llm_calls: NonNegativeInt
    incomplete_usage_reports: NonNegativeInt = 0
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    tokens_cached: NonNegativeInt
    tokens_reasoning: NonNegativeInt
    cache_hit_pct: float | None = Field(ge=0, le=100)


class HeartbeatLastPause(BaseModel):
    """The agent's most recent heartbeat pause — when it last opted out of idle
    check-ins and the length it asked for. Sourced from the newest
    durable heartbeat_pause_log row within the last 24 hours; None when no
    pause exists in that display window. `at` is when the pause was requested; `duration_s` is the
    requested window in seconds (the agent's `pause_heartbeat(duration)` arg)."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    duration_s: float = Field(ge=0)


# meiva:note
# One of four mutually-exclusive heartbeat display states — a pause, a queued wake, a projected
# check-in, or nothing — each with the rule and the daemon predicate behind it.
# meiva:link gateway/inspect/docs/heartbeat.md
class HeartbeatInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    interval_s: int
    next_at: datetime | None = None
    paused_until: datetime | None = None
    heartbeat_pending: bool = False
    last_pause: HeartbeatLastPause | None = None


class InspectorScheduleInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    enabled: bool
    status: str
    trigger_kind: Literal["at", "cron", "every"]
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: datetime | None = None
    next_launch_at: datetime | None = None
    owner_agent_id: int | None = None
    created_by_agent_id: int | None = None
    machine: str


class AgentInspectLive(BaseModel):
    """GET /api/agents/{id}/inspect/live response — the inspector's cheap,
    window-independent skeleton.

    Every field reflects database or runner state. ``heartbeat.last_pause``
    reads the indexed durable pause trail; no log query is performed. The response omits
    cost, stats, TPS, and activity so switching agents does not wait for the
    expensive event-history aggregate fan-out.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    machine: str
    status: str
    liveness_state: LivenessState
    last_probe_at: datetime | None = None
    observation: AgentObservation | None = None
    shells_available: bool | None = None
    spawned_at: datetime
    shells: list[ShellInfo]
    schedules: list[InspectorScheduleInfo] = Field(default_factory=list[InspectorScheduleInfo])
    config_overlay: dict[str, object]
    heartbeat: HeartbeatInfo


class AgentInspectStatistics(BaseModel):
    """Window-dependent usage of one agent, read from the usage ledger.

    ``window_hours`` identifies the requested view; ``applied_window_hours`` echoes the served
    hour window. Current state belongs exclusively to ``AgentInspectLive``.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window_hours: StatsWindowHours | None = None
    applied_window_hours: int | None = None
    cost: AgentCost


class NeighborRow(BaseModel):
    """One row in the GET /api/agents/{id}/neighbors result — a neighbor in
    `neighbors` or an ancestor in `ancestors`."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    status: str
    # Hops from the queried agent: out along ties for `neighbors`, up the
    # spawn chain for `ancestors` (1 = direct).
    depth: int
    # Tie strength, discounted per extra hop. Higher = closer. For ancestors:
    # the lineage edge weight (spawn/fork counts only).
    score: float


class NeighborsResponse(BaseModel):
    """GET /api/agents/{id}/neighbors response."""

    model_config = ConfigDict(frozen=True)

    # Undirected ties ranked by recency-weighted strength, strongest first.
    neighbors: list[NeighborRow]
    # The spawn/fork chain above the queried agent, nearest ancestor first.
    ancestors: list[NeighborRow]


class BornChainRow(BaseModel):
    """One ancestor in GET /api/agents/{id}/born-chain — the immutable birth
    chain above the queried agent, nearest ancestor first (1 = direct birth
    parent). Carries what the inherited-memory context note needs (`machine`
    decides which ancestors it can read locally); no tie score, because this
    route reads no tie graph."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    status: str
    machine: str | None
    depth: int


class BornChainResponse(BaseModel):
    """GET /api/agents/{id}/born-chain response."""

    model_config = ConfigDict(frozen=True)

    # The spawn/fork chain above the queried agent, nearest ancestor first.
    ancestors: list[BornChainRow]
