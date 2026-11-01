# meiva:note
# Liveness + system status panel + dashboard endpoints.
# - `/api/health` — liveness probe (public allowlist; pings DB)
# - `/api/stats/dashboard` — sidebar Status popover
# - `/api/status` — services / shells / cluster panel
#
# Cluster probe sub-fan-out lives here because /api/status is the
# consumer; agent-runner probes go through a `status_probe` op
# round-trip so it stays at constant wall-time regardless of N machines.
# meiva:link gateway/cluster/docs/ops-surfaces.meiva.okf.md

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg import Cursor
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.api_contracts.status import MachineStatus
from base.clock import Clock
from base.cluster.machine import (
    MachineIdentityReader,
)
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.deploy.git.cluster_drift import checkout_head_sha
from base.deploy.lifecycle import start_serving
from base.host.resource.reading import ResourceSample
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugins.surface import stats
from gateway.cluster._health import get_health
from gateway.cluster.monitor import stats_events
from gateway.cluster.monitor.alert_classes import read_alert_classes
from gateway.cluster.roster import roster_probe, roster_rows
from gateway.cluster.roster.liveness_snapshot import Snapshot
from gateway.cluster.roster.roster_plan import (
    RosterStep,
    SnapshotVerdict,
    StepKind,
    plan_roster,
    snapshot_verdict,
)
from gateway.cluster.roster.roster_rows import LocalFacts, RosterRowSpec
from gateway.cluster.roster.snapshots import read_all, readroster_rows
from gateway.cluster.schemas import (
    ClusterPanel,
    PluginStat,
    ServiceItem,
    ServicesStatus,
    StatsDashboard,
    StatsTokens,
    SystemStatus,
)
from gateway.http.config import (
    GatewayConfig,
    gateway_clock,
    gateway_cluster,
    gateway_config,
    gateway_rpc,
)
from gateway.schemas.stats import StatsWindowHours, window_delta
from ops.cluster import rpc as cluster_rpc
from ops.cluster.rpc import RpcClient
from ops.cluster_status import ClusterStatus, check_pidfile, serving_gate_reason
from ops.cluster_status.schema_mismatch import status as schema_mismatch_status

router = APIRouter()
_log = logging.getLogger(__name__)
_STATUS_CACHE_TTL_S = 15.0


class StatusCache:
    """Single-flight TTL cache of the `/api/status` response, one per gateway process.

    Built by the app lifespan. Probe wall time follows the slowest machine and several
    frontend pollers request this roster, so one caller recomputes while the others wait
    on the lock and then read what it stored.
    """

    def __init__(self, *, ttl_s: float = _STATUS_CACHE_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._cached: tuple[float, SystemStatus] | None = None
        self._lock = threading.Lock()

    def get(self, compute: Callable[[], SystemStatus]) -> SystemStatus:
        """The cached response while it is fresh, else the result of `compute()`."""
        cached = self._cached
        if cached is not None and time.monotonic() - cached[0] < self._ttl_s:
            return cached[1]
        with self._lock:
            cached = self._cached
            if cached is not None and time.monotonic() - cached[0] < self._ttl_s:
                return cached[1]
            response = compute()
            self._cached = (time.monotonic(), response)
            return response


router.add_api_route("/api/health", get_health, methods=["GET"], response_model=None)


@router.get(
    "/api/stats/dashboard",
    description="Pull all data for the sidebar Status popover in one shot.\n\nData sources:\n- `live_count`: agents_meta table — all non-terminated agents (running/idling)\n- `tokens` / `cost_usd` / average turn duration: the window's `llm_usage` and\n  `turn_end` rows in `telemetry_events` (cost is each row's usage-time snapshot)\n- warning/error event totals and class counts: the SELECTED window's warning, error and\n  critical `telemetry_events` rows grouped by class; a class is dismissed when an active\n  `event_dismissals` row cancels it (`gateway.cluster.monitor.alert_classes`)\n\n`?hours=` selects the aggregation window (0 = last 5m; 1/6/24/72/168 =\nhours), whitelisted by `StatsWindowHours` (anything else 422s); the served horizon is\n`applied_window_hours`, which is the requested window. Zero-data scenario: tokens all 0,\ncost_usd 0.0, avg_turn_seconds None (frontend shows \"—\"). The window is computed on\nevery request, in one connection, with an 8-second statement timeout.",
)
def get_stats_dashboard(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> StatsDashboard:
    try:
        return _compute_stats_dashboard(
            request.app.state.db_pool,
            hours,
            request.app.state.gateway_config,
            gateway_clock(request),
            cluster=gateway_cluster(request),
        )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="stats read timed out; retry") from exc


def _compute_stats_dashboard(
    pool: ConnectionPool[Any],
    hours: StatsWindowHours,
    config: GatewayConfig,
    clock: Clock,
    *,
    cluster: str,
) -> StatsDashboard:
    """Assemble the payload from one pooled connection."""

    now = clock.now()
    window_start = now - window_delta(hours)
    with pool.connection() as conn:
        stats_events.set_statement_timeout(conn, config.stats_statement_timeout_ms)
        totals = stats_events.window_totals(conn, cluster=cluster, start=window_start, end=now)
        alert_classes = read_alert_classes(conn, cluster=cluster, start=window_start, end=now)
        live_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM agents_meta WHERE status != 'terminated'"
            ).fetchone()[  # type: ignore[index]
                0
            ]
        )
    cache_hit_pct = (
        round(totals.cache_read / totals.cache_input * 100, 2)
        if totals.cache_input
        else (None if totals.in_total else 0.0)
    )
    return StatsDashboard(
        live_count=live_count,
        window_hours=hours,
        applied_window_hours=int(hours),
        tokens=StatsTokens(
            input=totals.in_total,
            output=totals.out_total,
            cache_read=totals.cache_read,
            cache_hit_pct=cache_hit_pct,
        ),
        cost_usd=totals.cost_usd,
        avg_turn_seconds=totals.turn_seconds / totals.turn_count if totals.turn_count else None,
        warnings=sum(row.count for row in alert_classes if row.level == "warning"),
        errors=sum(row.count for row in alert_classes if row.level != "warning"),
        alert_classes_active=sum(row.dismissal_id is None for row in alert_classes),
        alert_classes_dismissed=sum(row.dismissal_id is not None for row in alert_classes),
        plugin_stats=_plugin_stat_rows(pool),
        as_of=clock.now(),
    )


# meiva:note
# The runtime values behind plugin-declared status cards, for the response.
# Not windowed: a plugin value is a point in time (`PluginStat`), and the console
# joins these rows against `contributions.ui.stats` declarations by `(plugin, id)`.
# meiva:link gateway/cluster/docs/ops-surfaces.meiva.okf.md
def _plugin_stat_rows(pool: ConnectionPool[Any]) -> list[PluginStat]:
    return [
        PluginStat(
            plugin=row.plugin,
            id=row.id,
            value=row.value,
            detail=row.detail,
            status=row.status,  # pyright: ignore[reportArgumentType]
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )
        for row in stats.read_all(pool)
    ]


def get_services_status(endpoints: ServiceEndpoints) -> ServicesStatus:
    """Gateway-only daemon health (pidfile + signal).

    Per-host daemons (agent-host, watchdog) are not here — they ride each
    machine's ClusterStatus probe and render in the roster. This block is the
    daemons that only run on the gateway."""
    items: list[ServiceItem] = []
    for name, label in (("labeler", "Labeler Daemon"), ("memory_indexer", "Memory Indexer")):
        alive, pid = check_pidfile(str(endpoints.of(name).pidfile))
        items.append(roster_rows.service_item(name, label, alive=alive, pid=pid))
    return ServicesStatus(items=items)


# meiva:note
# What one roster read needs from the gateway process, read once by the handler.
#
# `probe_timeout_s` is the budget of a dial the gateway makes itself (a fresh read, or a
# machine the heartbeat snapshot does not cover): `gateway.status_probe_timeout_seconds`,
# default 8s. Raised from a 3.0s hardcode (task #1200): a slow-but-healthy runner's
# status_snapshot measured 3.07-3.27s on 2026-08-12, and a budget shorter than the handler's
# own wall time flipped it offline while /healthz answered in ~15ms. The default read dials
# nothing: it renders the heartbeat liveness pass's snapshot, which reads this same setting
# (services/wake/heartbeat/liveness.py), so the pass's probes stay aligned with a fresh
# read's.
@dataclass(frozen=True)
class RosterEnv:
    db: Database
    identity_log: roster_probe.IdentityMismatchLog
    image: LoadedCommit
    home: Path
    machine: MachineIdentityReader
    probe_timeout_s: float
    clock: Clock
    rpc: RpcClient
    observe: Callable[[Path], start_serving.RootBirth]
    environ: Mapping[str, str] = field(repr=False)


def roster_env(request: Request) -> RosterEnv:
    return RosterEnv(
        db=request.app.state.db,
        identity_log=request.app.state.identity_mismatch_log,
        image=request.app.state.process_image,
        home=request.app.state.gateway_process.home,
        machine=request.app.state.machine,
        probe_timeout_s=gateway_config(request).status_probe_timeout_seconds,
        clock=gateway_clock(request),
        rpc=gateway_rpc(request),
        observe=request.app.state.gateway_process.observe,
        environ=request.app.state.gateway_process.config.process_environment(),
    )


# meiva:note
# Validate a host's `status_probe` answer as the ClusterStatus contract and render it.
#
# A body that does not validate (a version-skewed / wrong server) must NOT be coerced into
# a determinate paused verdict: it lands in the documented online=True + paused=None
# abnormal state instead of a false green. An answer from another host is an identity
# mismatch; its log line is episode-deduped and degrades to INFO for a stopped row (a stale
# URL answering for someone else is that row's expected face — task #4143).
def _render_answer(
    env: RosterEnv,
    row: RosterRowSpec,
    answer: Mapping[str, object],
    *,
    observed_at: datetime | None,
) -> MachineStatus:
    try:
        status = ClusterStatus.model_validate(answer)
    except ValidationError:
        _log.warning(
            "status_probe answer for %r does not match ClusterStatus; reporting online+unknown",
            row.name,
            exc_info=True,
        )
        return roster_rows.reachable_unknown_status(row)
    if status.machine_name != row.name:
        env.identity_log.log_mismatch(
            row.name, row.gateway_url, status.machine_name, stopped=row.stopped_at is not None
        )
    else:
        env.identity_log.note_match(row.name)
    return roster_rows.reported_status(
        row, roster_probe.probed_facts(status), observed_at=observed_at
    )


# meiva:note
# Render a roster row from the heartbeat pass's last probe of the machine.
def _snapshot_row(env: RosterEnv, row: RosterRowSpec, snapshot: Snapshot) -> MachineStatus:
    match snapshot_verdict(snapshot):
        case SnapshotVerdict.OFFLINE:
            return roster_rows.offline_status(row)
        case SnapshotVerdict.UNKNOWN:
            return roster_rows.reachable_unknown_status(row)
        case SnapshotVerdict.REPORTED:
            if snapshot.status is None:
                raise RuntimeError("a reported snapshot carries a status")
            return _render_answer(
                env, row, snapshot.status, observed_at=snapshot.status_at or snapshot.observed_at
            )


# meiva:note
# Probe an agent-runner now, by POSTing a `status_probe` op to its ops server.
#
# The machine is reached at its meiva-ops server (services/agent_runner/agent_ops), which
# dispatches `status_probe` via `ops.cluster.operations.cluster_status_op` in-process and
# returns the snapshot. Same path the heartbeat liveness pass uses. The local
# machine is no special case — its ops server is dialed at its registered
# localhost URL, keeping one uniform probe path.
#
# Online == the ops server responded within the timeout. Paused comes from the
# host's own `status_probe` snapshot. Nothing is remembered: a down host
# costs this dial its full budget every time, which is why the default read does
# not come here.
async def probe_agent_runner(env: RosterEnv, row: RosterRowSpec) -> MachineStatus:
    if row.gateway_url is None:
        # The roster row is the address authority for this fan-out. Do not let
        # cluster_rpc synchronously re-read Postgres outside the async timeout.
        return roster_rows.offline_status(row)
    try:
        result = await roster_probe.dispatch_status_probe(
            row.name, row.gateway_url, timeout_s=env.probe_timeout_s, rpc=env.rpc
        )
    except cluster_rpc.ClusterOpUnreachable:
        # Expected when a host is genuinely offline / mid-restart — quiet.
        return roster_rows.offline_status(row)
    except cluster_rpc.ClusterOpFailed as exc:
        # Reached the ops server, but its status_probe op itself raised (DB error,
        # schema drift inside the op). That is NOT "offline" — surface it so the
        # real error is not invisible behind a misleading offline marker.
        _log.warning("status_probe op failed on reachable host %s: %s", row.name, exc.result)
        return roster_rows.reachable_unknown_status(row)
    return _render_answer(env, row, result, observed_at=None)


# meiva:note
# One live resource reading for the gateway's own machine (no status_snapshot call).
def _local_resource_sample() -> ResourceSample | None:
    import psutil

    from base.host.resource.sample import resource_sample

    try:
        return resource_sample()
    except (OSError, psutil.Error):
        # An unreadable disk / process table degrades the row to no resource figures.
        return None


# meiva:note
# What the gateway's own machine reports about itself — via to_thread: the serving gate
# (file read), the checkout HEAD (git rev-parse subprocess), the frozen process commit and
# the psutil resource snapshot must not run on the event loop.
def read_local_facts(env: RosterEnv, *, with_resource: bool) -> LocalFacts:
    return LocalFacts(
        paused_reason=serving_gate_reason(
            serving=start_serving.is_serving(env.home, observe=env.observe)
        ),
        head_sha=checkout_head_sha(env.home / "source", environ=env.environ),
        # meiva:note
        # This gateway process's own frozen commit — not a disk bookmark, so a
        # gateway that outlived a checkout advance reports the old commit and the
        # roster shows the drift.
        running_sha=env.image.sha,
        schema_mismatch=schema_mismatch_status(env.db, local_machine=env.machine.name()),
        resource=_local_resource_sample() if with_resource else None,
    )


async def _execute(env: RosterEnv, step: RosterStep) -> MachineStatus:
    match step.kind:
        case StepKind.LOCAL:
            facts = await asyncio.to_thread(read_local_facts, env, with_resource=True)
            return roster_rows.local_status(step.row, facts)
        case StepKind.SNAPSHOT:
            if step.snapshot is None:
                raise RuntimeError("a snapshot step carries its snapshot")
            return _snapshot_row(env, step.row, step.snapshot)
        case StepKind.DIAL:
            return await probe_agent_runner(env, step.row)


# meiva:note
# The roster of the given machines, as `plan_roster` lays it out.
#
# With `snapshots` (the default read) each machine renders from the heartbeat
# liveness pass's last probe (`gateway/cluster/roster/snapshots.py`) and nothing is dialed,
# so no down host can drag the read (task #3507); a machine with no fresh snapshot
# is dialed here, as a degraded fallback for a heartbeat service that is down. With
# `snapshots=None` (an explicit fresh read) every machine is probed in parallel via
# a status_probe op to its ops server (the local machine included — its ops server
# is dialed at its registered localhost URL); total wall ≈
# `gateway.status_probe_timeout_seconds`.
#
# The one exception is a local machine without the agent-runner capability
# (a pure gateway in a split deployment): it runs no ops server, so its row
# is a lightweight local read with no session/pidfile probes (shell/daemon
# liveness is agent-runner data a pure gateway does not have).
#
# Public because `gateway/cluster/router.py:get_cluster_machines` reuses the same fan-out to
# back meiva.agents.list_machines(). The rows come back sorted by machine name.
async def gather_cluster_status(
    env: RosterEnv, rows: Sequence[RosterRowSpec], snapshots: Mapping[str, Snapshot] | None
) -> list[MachineStatus]:
    steps = plan_roster(rows, env.machine.name(), snapshots, now=env.clock.now())
    machines = await asyncio.gather(*(_execute(env, step) for step in steps))
    return sorted(machines, key=lambda m: m.name)


# meiva:note
# Assemble the cluster sub-section: SELECT the machines table (paused rows
# excluded — the cluster panel shows only active members; `meiva cluster resume`
# brings a row back) and render each machine from the heartbeat liveness
# pass's last `status_probe` (`gateway/cluster/roster/snapshots.py`), so the panel
# dials no runner (task #3507).
#
# Wrapped sync via asyncio.run because `/api/status` is a sync FastAPI
# handler (runs in threadpool); creating a fresh event loop here is safe.
# asyncio.run() fully closes the loop (and its selector fds) before returning,
# so concurrent panel polls each get a short-lived loop with no accumulation —
# the per-call loop construction is the only cost, negligible at panel cadence.
# Migrate the handler to `async def` only if that cadence rises enough to make
# loop setup measurable.
def get_cluster_status(env: RosterEnv, cur: Cursor) -> ClusterPanel:
    rows = readroster_rows(cur)
    snapshots = read_all(cur)
    machines = asyncio.run(gather_cluster_status(env, rows, snapshots)) if rows else []
    role = env.machine.role()
    return ClusterPanel(
        current_machine=env.machine.name(),
        current_serve_gateway=("gateway" in role),
        current_serve_agent_runner=("agent-runner" in role),
        current_serve_observability_station=("observability-station" in role),
        current_paused=serving_gate_reason(
            serving=start_serving.is_serving(env.home, observe=env.observe)
        )
        is not None,
        machines=machines,
    )


@router.get("/api/status")
def get_system_status(request: Request) -> SystemStatus:
    """System status panel — pull services / shells / cluster in one shot.

    Probe wall time follows the slowest machine, while multiple frontend pollers
    request this roster. Fifteen-second staleness is acceptable for diagnostics;
    single-flight prevents expiry stampedes.
    Each block queries independently — a single failure does not affect
    the others (each has its own try/except that falls back to a
    degraded value).
    """
    status_cache: StatusCache = request.app.state.status_cache
    return status_cache.get(lambda: _compute_system_status(request))


def _compute_system_status(request: Request) -> SystemStatus:
    """One uncached build of the status panel."""
    # meiva:note
    # The request's own facts are read once here and travel down as values: both the roster leg
    # and the degraded panel below render the same observation of this host.
    env = roster_env(request)
    # Services
    try:
        services = get_services_status(request.app.state.service_endpoints)
    except Exception:
        _log.exception("GET /api/status: services check failed")
        services = ServicesStatus(items=[])

    # Cluster
    try:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cluster = get_cluster_status(env, cur)
    except Exception:
        _log.exception("GET /api/status: cluster query failed")
        # Fallback: at least surface this host's name/role so the frontend
        # does not lose the whole section.
        try:
            cluster = ClusterPanel(
                current_machine=request.app.state.machine.name(),
                current_serve_gateway=("gateway" in request.app.state.machine.role()),
                current_serve_agent_runner=("agent-runner" in request.app.state.machine.role()),
                current_serve_observability_station=(
                    "observability-station" in request.app.state.machine.role()
                ),
                current_paused=serving_gate_reason(
                    serving=start_serving.is_serving(env.home, observe=env.observe)
                )
                is not None,
                machines=[],
            )
        except Exception:
            _log.exception("GET /api/status: cluster fallback failed")
            cluster = ClusterPanel(
                current_machine="?",
                current_serve_gateway=False,
                current_serve_agent_runner=False,
                current_paused=False,
                machines=[],
            )

    return SystemStatus(services=services, cluster=cluster)
