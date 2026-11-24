# meiva:note
# Wire models of the cluster surface: system + cluster status and the
# machine pause/resume/delete admin responses and the stats-dashboard aggregate.
#
# `MachineStatus` (the roster row the CLI also decodes) lives in
# `base.api_contracts.status` so `cli` can decode the roster without importing
# up into `gateway`; the models below are the gateway-only status surface.
#
# FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
# the wire before.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
)

from base.api_contracts.status import MachineStatus
from base.packages.plugins.surface.stats_types import StatReset
from gateway.schemas.models import (
    HarnessAvailability as HarnessAvailability,
)
from gateway.schemas.models import (
    HarnessModelOption as HarnessModelOption,
)
from gateway.schemas.stats import StatsWindowHours


class ServiceItem(BaseModel):
    """Online status of a single daemon."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    online: bool | None  # None = unknown (cannot probe)
    pid: int | None
    detail: str | None


class ServicesStatus(BaseModel):
    """GET /api/status services sub-section."""

    model_config = ConfigDict(frozen=True)

    items: list[ServiceItem]


class ClusterPanel(BaseModel):
    """GET /api/status cluster sub-section — multi-machine view.

    `current_*` is the perspective of the gateway that received this
    request; `machines` is every machine registered in DB + live probe
    results.
    """

    model_config = ConfigDict(frozen=True)

    current_machine: str
    # This host's three capability flags (any combination on a single-box host).
    # current_serve_observability_station defaults False so pre-station clients
    # parse the payload.
    current_serve_gateway: bool
    current_serve_agent_runner: bool
    current_serve_observability_station: bool = False
    # meiva:note
    # Whether the current gateway's serving gate has not reached `serving`.
    current_paused: bool
    machines: list[MachineStatus]


class SystemStatus(BaseModel):
    """GET /api/status response — System Status panel data all in one go."""

    model_config = ConfigDict(frozen=True)

    services: ServicesStatus
    cluster: ClusterPanel


class AgentMachineRow(BaseModel):
    """One machine as exposed to agents via meiva.agents.list_machines().

    Intentionally minimal: name + free-text description + determinate liveness.
    A paused host is still live, but a reached host whose status operation
    failed has no liveness verdict, so `live=False` until a probe returns a
    concrete paused value. role / gateway_url stay internal to ops and are not
    surfaced to agents.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None = None
    live: bool
    # Operator-set staging flag — agents see it so a peer can tell a staging
    # host from a production rollout target (e.g. before enrolling against it).
    is_staging: bool = False


class MachineDeleteResponse(BaseModel):
    """DELETE /api/cluster/machines/{name} response."""

    model_config = ConfigDict(frozen=True)

    deleted: bool  # True = row existed and was removed; False = already absent


class MachinePauseRequest(BaseModel):
    """POST /api/cluster/machines/{name}/pause body.

    `reason` is free-text why the machine is being pulled out (e.g. "a week off")
    — recorded on the machines row as `pause_reason` for the resume checklist.
    """

    reason: str = ""


class MachinePauseResponse(BaseModel):
    """POST /api/cluster/machines/{name}/pause response — what the pause did.

    The pause sets the admission latch, then force-terminates the machine's agents
    through its ops server. When the server cannot take the request, the gateway
    force-marks the agent terminated in the shared DB.
    `paused_at`/`pause_reason` are the row values after the latch write.
    Idempotent: pausing an already-paused machine terminates nothing (its
    agents are already terminated) and returns the existing latch values.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    paused: bool  # True = the machine now carries the pause latch
    terminated_agents: int  # killed + marked terminated via the machine's ops server
    force_marked_agents: int  # ops unreachable; row force-marked terminated in the DB
    paused_at: datetime | None = None
    pause_reason: str | None = None


class MachineResumeResponse(BaseModel):
    """POST /api/cluster/machines/{name}/resume response.

    `resumed` True = the pause latch was cleared (the machine is a normal
    cluster member again); False = it was not paused (idempotent no-op).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    resumed: bool


class StatsTokens(BaseModel):
    """`/api/stats/dashboard` token sub-section — windowed LLM usage aggregation.

    `cache_hit_pct` uses only input with cache metering, rounded to 2 decimals;
    nonzero input with no cache metering produces null.
    input=0 degrades to 0 to avoid div-by-zero."""

    model_config = ConfigDict(frozen=True)

    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt
    cache_hit_pct: float | None = Field(ge=0, le=100)


# The plugin-stats status vocabulary, mirrored from `base.packages.plugins.surface.stats`
# (the DB-side writer + check constraint). There is deliberately no "empty":
# a card with no row IS the frontend's empty state, and a second spelling of
# that would be a second fact to keep in sync.
PluginStatStatus = Literal["ok", "warn", "error"]


# meiva:note
# One declared status-panel card's current value (task #2911).
# The declaration half (existence, label) arrives via
# `GET /api/ui/contributions` (`UiStatContribution`); this is the runtime
# half, keyed by `(plugin, id)` and joined against the declaration by the
# console. Values are NOT windowed: the window selector governs the
# console's own aggregates, while a usage meter's "current" is a
# point-in-time fact and would be meaningless averaged over a horizon.
# `updated_at` is when the plugin last wrote the row — the console renders
# its age, so a refresh that stopped running cannot pass for a fresh value.
# meiva:link gateway/cluster/docs/ops-surfaces.md
class PluginStat(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": (
                "One declared status-panel card's current value (task #2911).\n\n"
                "The declaration half (existence, label) arrives via\n"
                "`GET /api/ui/contributions` (`UiStatContribution`); this is the runtime\n"
                "half, keyed by `(plugin, id)` and joined against the declaration by the\n"
                "console. Values are NOT windowed: the window selector governs the\n"
                "console's own aggregates, while a usage meter's \"current\" is a\n"
                "point-in-time fact and would be meaningless averaged over a horizon.\n\n"
                "`updated_at` is when the plugin last wrote the row — the console renders\n"
                "its age, so a refresh that stopped running cannot pass for a fresh value."
            )
        },
    )

    plugin: str
    id: str
    value: str
    detail: str | None
    status: PluginStatStatus
    updated_at: datetime
    updated_by: str | None
    resets: list[StatReset] = Field(default_factory=list)


# meiva:note
# Point-in-time unresolved Grafana alert counts from `alerts`, matching `/api/alerts`.
class StatsAlerts(BaseModel):
    model_config = ConfigDict(frozen=True)

    unresolved: NonNegativeInt = 0
    critical: NonNegativeInt = 0
    error: NonNegativeInt = 0
    warning: NonNegativeInt = 0


# meiva:note
# GET /api/stats/dashboard response — the sidebar Status popover's data pulled in one shot.
# All windowed fields aggregate over the applied_window_hours horizon; `live_count`, `alerts`
# and `plugin_stats` are point-in-time live state.
# meiva:link gateway/cluster/docs/ops-surfaces.md
class StatsDashboard(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": (
                "GET /api/stats/dashboard response — the sidebar Status popover's data pulled in one shot.\n\n"
                "Point-in-time fields (`live_count`, `alerts`, `plugin_stats`) report live cluster state.\n"
                "Windowed fields (`tokens`, cost slices) aggregate over `applied_window_hours`.\n\n"
                "- `alerts`: live unresolved Grafana alerts from `alerts` (excluding synthetic watchdog rows), "
                "using the same single source of truth as `/api/alerts`."
            )
        },
    )

    live_count: NonNegativeInt
    window_hours: StatsWindowHours
    applied_window_hours: int | None = None
    tokens: StatsTokens
    cost_usd: float = Field(ge=0)
    subscription_cost_usd: float = Field(ge=0)
    usage_unpriced_calls: NonNegativeInt = 0
    usage_incomplete_reports: NonNegativeInt = 0
    openai_cost_usd: float | None = Field(default=None, ge=0)
    anthropic_cost_usd: float | None = Field(default=None, ge=0)
    google_cost_usd: float | None = Field(default=None, ge=0)
    alerts: StatsAlerts
    plugin_stats: list[PluginStat]
    as_of: datetime | None = None
