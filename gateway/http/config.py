"""Immutable configuration shared by one gateway application's HTTP consumers."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args, get_type_hints
from urllib.parse import urlsplit

from fastapi import Request

from base.agents.upload_delivery.paths import (
    MAX_AGENT_UPLOAD_BYTES,
    MAX_AGENT_UPLOAD_FILES,
    MAX_UPLOAD_BYTES,
)
from base.clock import Clock, ClockConfig
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.env.health_ports import health_port
from gateway.http.middleware.cors import cors_allowed_origins, trusted_request_hosts
from ops.cluster.rpc import RpcClient


# meiva:note
# The page sizes and windows a list or read endpoint uses when its query leaves one out.
# Each field is the `display` setting of the same name, read once by the root.
@dataclass(frozen=True)
class DisplayDefaults:
    shell_capture_default_lines: int
    config_audit_default_last: int
    fleet_graph_decay_lambda: float
    cluster_events_default_limit: int
    neighbors_default_depth: int
    neighbors_default_limit: int
    messages_default_limit: int
    notices_open_default_limit: int
    notices_resolved_default_page: int
    schedules_runs_default_limit: int
    alerts_default_limit: int
    metrics_default_window_days: int
    events_default_limit: int
    timeline_default_limit: int


# meiva:note
# The models a request falls back to: the default chat model, the hierarchy model and the
# `llm_override` that routes every model call of the cluster to one model when it is set.
@dataclass(frozen=True)
class ModelDefaults:
    llm_model: str
    hierarchy_model: str
    llm_override: str


# meiva:note
# The understanding options of the agent config domain. Both are `None` in a process profile
# that does not build the domain (the gateway's); the request then reads the cluster's value
# from the unit `.env` instead.
@dataclass(frozen=True)
class UnderstandingDomain:
    chunk_ratio: float | None
    enabled: bool | None


# meiva:note
# Server-sent-event tuning: how often a stream checks that its client is still connected, the
# events per second a throttled stream lets through, and how many events one channel may hold
# while its client is slow.
@dataclass(frozen=True)
class SseTuning:
    disconnect_poll_seconds: float
    throttle_rate: float
    channel_pending_limit: int


# meiva:note
# Alert intake: whether an alert also notifies the IM bridge, where the bridge answers
# (`im_bridge_url`, the base of its `/send/...` routes) and the scoped webhook token an alert
# sender presents (empty when no scoped token is configured).
@dataclass(frozen=True)
class AlertSettings:
    im_notify_enabled: bool
    im_bridge_url: str
    webhook_token: str = field(repr=False)


# meiva:note
# What a `to_user` document read may cost: the longest resource chain it follows and the
# largest resource it fetches.
@dataclass(frozen=True)
class ToUserLimits:
    max_chain_depth: int
    max_resource_bytes: int


# meiva:note
# The Grafana reverse proxy: off unless enabled, and the address it forwards to.
@dataclass(frozen=True)
class GrafanaProxy:
    enabled: bool
    host: str
    port: int


# meiva:note
# Where a delivered page is reachable (`gateway_url`, empty when none is configured) and how
# long a page lives when its author names no lifetime.
@dataclass(frozen=True)
class PageSettings:
    gateway_url: str
    default_ttl_seconds: float


# meiva:note
# The memory pool checkout policy of a refresh: keep local commits, and the branch to follow.
@dataclass(frozen=True)
class MemoryPoolPolicy:
    keep_local: bool
    track_branch: str


# meiva:note
# The memory-search tunables: the embedding provider and its key, the vector backend, the
# waits for the concurrency gate and for a whole search, and the candidate counts of the
# reranking and deduplication purposes.
@dataclass(frozen=True)
class MemorySearchSettings:
    embedding_backend: str
    backend: str
    uri: str
    embedding_timeout: float
    api_key: str | None = field(repr=False)
    acquire_timeout: float
    deadline: float
    rerank_model: str
    rerank_candidates: int
    dedup_candidates: int


# meiva:note
# How an agent's heartbeat is judged by the live inspection: the idle time after which an
# agent counts as idle and the interval at which it beats.
@dataclass(frozen=True)
class HeartbeatTiming:
    idle_threshold_seconds: float
    interval_seconds: float


@dataclass(frozen=True)
class GatewayConfig:
    """Restart-bound HTTP policy derived from the gateway process's config owner."""

    cluster_secret: str = field(repr=False)
    session_ttl_seconds: int
    mcp_endpoint_enabled: bool
    allowed_origins: tuple[str, ...]
    browser_origin: str
    session_cookie_secure: bool
    memory_root: Path
    # meiva:note
    # The account home under which uploads live (`Downloads/MeivaAgent-<id>`): the root reads
    # it once, so a handler and a test hand the same directory around as a value.
    upload_home: Path
    human_auth: Literal["required", "trusted_network"]
    # meiva:note
    # The settings the request handlers read, grouped by what they tune. The root reads each
    # from the process's configuration owner once; a handler reads only this value, never the
    # owner, and a changed `.env` reaches a handler at the next restart like every other field
    # here.
    display: DisplayDefaults
    models: ModelDefaults
    understanding: UnderstandingDomain
    sse: SseTuning
    alerts: AlertSettings
    to_user: ToUserLimits
    grafana: GrafanaProxy
    pages: PageSettings
    memory_pool: MemoryPoolPolicy
    memory_search: MemorySearchSettings
    heartbeat: HeartbeatTiming
    status_probe_timeout_seconds: float
    notice_ttl_limit_seconds: float
    timeline_compact_history: int
    message_timestamps: bool
    clock: ClockConfig
    # meiva:note
    # The entropy this gateway app draws request correlation ids, attempt ids, upload batch ids
    # and the session MAC key from. The root builds it once (a CSPRNG in production), so a
    # handler asks the config for the value it needs and a test hands in a seeded generator. It
    # is a resource, not a setting: it takes no part in comparing two configs.
    rng: random.Random = field(repr=False, compare=False)
    trusted_hosts: frozenset[str] = frozenset()
    # meiva:note
    # How long a lifecycle request (resurrect / terminate / restart) may wait on the home
    # runner. Browser clients give such a request a short window, so an offline runner must
    # become a clear 502 inside it: one attempt is bounded, one retry rides a transient
    # connection drop, and the whole wait is bounded again.
    lifecycle_deadline_seconds: float = 12.0
    lifecycle_attempt_timeout_seconds: float = 5.0
    lifecycle_retries: int = 1
    # meiva:note
    # A read of the unified or the per-agent event history past this many milliseconds is
    # cancelled and answered as a retriable 503.
    events_read_timeout_ms: int = 10_000
    # meiva:note
    # How the page reverse proxy dials a page server: the connect fails fast when the server
    # is down, and the read bound is between chunks, so a live SSE stream or a slow large
    # download keeps flowing while a hung backend is severed.
    page_proxy_connect_seconds: float = 5.0
    page_proxy_read_seconds: float = 120.0
    # meiva:note
    # Connections the request handlers share. A request that holds one and asks for another
    # (a nested checkout) deadlocks a pool this small, which is what a test sets it to.
    db_pool_max_size: int = 8
    # meiva:note
    # The sidebar stats card, the alert-class list and the ops monitor read a window of
    # `telemetry_events`; a read past this many milliseconds is cancelled and answered as a
    # retriable 503.
    stats_statement_timeout_ms: int = 8000
    # meiva:note
    # Rows of the alert-class list, and newest events shown for one class.
    alert_classes_limit: int = 200
    alert_samples_limit: int = 5
    # meiva:note
    # First-contact per-host budget for a remote inventory_read in the aggregate fan-out, so
    # one offline host doesn't stall the whole matrix on the default deadline.
    inventory_host_wait_seconds: float = 15.0
    # meiva:note
    # How long a schedule start / stop / restart / edit / delete waits for the schedule-manager
    # service to consume its sync request (the service's one-second poll plus the kill and
    # relaunch it runs), looking this often. A service that is down is answered after the wait.
    schedule_consume_wait_seconds: float = 8.0
    schedule_consume_poll_seconds: float = 0.2
    # meiva:note
    # The upload write limits: one file, one agent's total bytes, one agent's file count. The
    # constants stay owned by `base.agents.upload_delivery.paths`; a gateway is built with them
    # and a test builds one with a small limit. The root never reads them from settings, so a
    # cluster cannot tune a limit away.
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_agent_upload_bytes: int = MAX_AGENT_UPLOAD_BYTES
    max_agent_upload_files: int = MAX_AGENT_UPLOAD_FILES
    # meiva:note
    # Whether the gateway runs the delivered-upload recovery loop in the background. A test
    # that drives the rounds itself, one batch at a time, turns it off.
    upload_recovery_enabled: bool = True


# meiva:note
# The root reads the account home and the memory directory under it and builds the entropy source;
# this function only records them beside the settings it snapshots.
def gateway_config_from_boot(
    config: ConfigBoot, *, user_home: Path, memory_root: Path, rng: random.Random
) -> GatewayConfig:
    """Snapshot HTTP policy without creating or booting another config owner."""
    view = config.view
    gateway = view.gateway
    services = view.services
    gateway_url: str = gateway.gateway_url
    machine = ConfiguredMachineIdentity(config)
    explicit_secure: bool | None = gateway.session_cookie_secure
    allowed_origins = tuple(
        cors_allowed_origins(
            explicit=gateway.cors_allowed_origins,
            frontend_healthcheck_url=services.frontend_healthcheck_url,
            app_port=services.app_port,
            browser_origin=gateway.browser_origin,
            gateway_url=gateway_url,
        )
    )
    display = view.display
    lm = view.lm
    daemon = view.daemon
    alerts_token = view.alerts.webhook_token
    gemini_key = lm.gemini_api_key
    # meiva:note
    # The gateway profile builds no agent domain; its understanding options then come from the
    # unit `.env` at the request (see `agents/history/configuration.py`).
    agent = view.agent if view.has_domain("agent") else None
    return GatewayConfig(
        cluster_secret=view.data_plane.cluster_secret,
        session_ttl_seconds=gateway.session_ttl_seconds,
        mcp_endpoint_enabled=gateway.mcp_endpoint_enabled,
        allowed_origins=allowed_origins,
        browser_origin=gateway.browser_origin,
        session_cookie_secure=(
            explicit_secure
            if explicit_secure is not None
            else urlsplit(gateway_url).scheme == "https"
        ),
        memory_root=memory_root,
        upload_home=user_home,
        human_auth=gateway.human_auth,
        display=DisplayDefaults(
            shell_capture_default_lines=display.shell_capture_default_lines,
            config_audit_default_last=display.config_audit_default_last,
            fleet_graph_decay_lambda=display.fleet_graph_decay_lambda,
            cluster_events_default_limit=display.cluster_events_default_limit,
            neighbors_default_depth=display.neighbors_default_depth,
            neighbors_default_limit=display.neighbors_default_limit,
            messages_default_limit=display.messages_default_limit,
            notices_open_default_limit=display.notices_open_default_limit,
            notices_resolved_default_page=display.notices_resolved_default_page,
            schedules_runs_default_limit=display.schedules_runs_default_limit,
            alerts_default_limit=display.alerts_default_limit,
            metrics_default_window_days=display.metrics_default_window_days,
            events_default_limit=display.events_default_limit,
            timeline_default_limit=display.timeline_default_limit,
        ),
        models=ModelDefaults(
            llm_model=lm.llm_model,
            hierarchy_model=lm.hierarchy_model,
            llm_override=lm.llm_override,
        ),
        understanding=UnderstandingDomain(
            chunk_ratio=None if agent is None else agent.understanding_chunk_ratio,
            enabled=None if agent is None else agent.understanding_enabled,
        ),
        sse=SseTuning(
            disconnect_poll_seconds=gateway.sse_disconnect_poll_seconds,
            throttle_rate=gateway.sse_throttle_rate,
            channel_pending_limit=gateway.sse_channel_pending_limit,
        ),
        alerts=AlertSettings(
            im_notify_enabled=view.alerts.im_notify_enabled,
            im_bridge_url=(services.im_bridge_health_url or "").rstrip("/")
            or f"http://127.0.0.1:{health_port('im_bridge', read_field=config.get_field)}",
            webhook_token="" if alerts_token is None else alerts_token.get_secret_value(),
        ),
        to_user=ToUserLimits(
            max_chain_depth=view.to_user.max_chain_depth,
            max_resource_bytes=view.to_user.max_resource_bytes,
        ),
        grafana=GrafanaProxy(
            enabled=gateway.grafana_proxy_enabled,
            host=gateway.grafana_host,
            port=gateway.grafana_port,
        ),
        pages=PageSettings(
            gateway_url=gateway_url, default_ttl_seconds=daemon.page_default_ttl_seconds
        ),
        memory_pool=MemoryPoolPolicy(
            keep_local=view.general.memory_keep_local, track_branch=view.general.track_branch
        ),
        memory_search=MemorySearchSettings(
            embedding_backend=services.embedding_backend,
            backend=services.memory_search_backend,
            uri=services.memory_search_uri,
            embedding_timeout=services.memory_embed_timeout_seconds,
            api_key=None if gemini_key is None else gemini_key.get_secret_value(),
            acquire_timeout=services.memory_search_acquire_timeout_seconds,
            deadline=services.memory_search_deadline_seconds,
            rerank_model=services.memory_rerank_model,
            rerank_candidates=services.memory_rerank_candidates,
            dedup_candidates=services.memory_dedup_candidates,
        ),
        heartbeat=HeartbeatTiming(
            idle_threshold_seconds=daemon.heartbeat_idle_threshold_seconds,
            interval_seconds=daemon.heartbeat_interval_seconds,
        ),
        status_probe_timeout_seconds=gateway.status_probe_timeout_seconds,
        notice_ttl_limit_seconds=daemon.notice_ttl_limit_seconds,
        timeline_compact_history=gateway.timeline_compact_history,
        message_timestamps=view.general.message_timestamps,
        clock=clock_config_from_boot(config),
        rng=rng,
        trusted_hosts=trusted_request_hosts(
            allowed_origins=allowed_origins,
            machine_host=machine.host(),
            gateway_url=gateway_url,
        ),
    )


_PLACEHOLDERS: dict[object, object] = {int: 0, float: 0.0, bool: False, str: ""}


# meiva:note
# One group filled with placeholders: `0`, `0.0`, `False` or `""` by field type, `None` for an
# optional field. A field of any other type fails here rather than getting a made-up value.
def _placeholders[G](group: type[G]) -> G:
    values: dict[str, object] = {}
    for name, hint in get_type_hints(group).items():
        if hint in _PLACEHOLDERS:
            values[name] = _PLACEHOLDERS[hint]
        elif type(None) in get_args(hint):
            values[name] = None
        else:
            raise TypeError(f"{group.__name__}.{name} has no placeholder for {hint!r}")
    return group(**values)


# meiva:note
# A config whose settings are all placeholders, for code that must not read the configuration:
# the OpenAPI document (the schema of a route does not depend on a setting value) and a
# stand-in app of a test, which sets, through `dataclasses.replace`, the few fields the code
# under test reads. No handler may read a placeholder.
def placeholder_config() -> GatewayConfig:
    return GatewayConfig(
        cluster_secret="",
        session_ttl_seconds=3600,
        mcp_endpoint_enabled=False,
        allowed_origins=(),
        browser_origin="",
        session_cookie_secure=False,
        human_auth="required",
        memory_root=Path("/nonexistent/memory"),
        upload_home=Path("/nonexistent/home"),
        display=_placeholders(DisplayDefaults),
        models=_placeholders(ModelDefaults),
        understanding=_placeholders(UnderstandingDomain),
        sse=_placeholders(SseTuning),
        alerts=_placeholders(AlertSettings),
        to_user=_placeholders(ToUserLimits),
        grafana=_placeholders(GrafanaProxy),
        pages=_placeholders(PageSettings),
        memory_pool=_placeholders(MemoryPoolPolicy),
        memory_search=_placeholders(MemorySearchSettings),
        heartbeat=_placeholders(HeartbeatTiming),
        status_probe_timeout_seconds=0.0,
        notice_ttl_limit_seconds=0.0,
        timeline_compact_history=0,
        message_timestamps=False,
        clock=_placeholders(ClockConfig),
        rng=random.Random(0),  # noqa: S311
    )


# meiva:note
# The config the gateway app was built with: the one frozen value a handler reads its settings
# from.
def gateway_config(request: Request) -> GatewayConfig:
    return request.app.state.gateway_config


# meiva:note
# The clock the gateway app was built with: its configured timezone over the system timers. The
# app root builds it once beside the config, so a handler asks for time instead of creating a clock.
def gateway_clock(request: Request) -> Clock:
    return request.app.state.clock


# meiva:note
# The cluster RPC client the gateway app serves with: its dispatch budget and gateway access come
# from the booted process configuration, over the system timers. The app's lifespan builds it once
# after the configuration is booted (a cold app, as the schema document builds, has none), so a
# handler asks for the client instead of creating one from process settings.
def gateway_rpc(request: Request) -> RpcClient:
    return request.app.state.rpc
