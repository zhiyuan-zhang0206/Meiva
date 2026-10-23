"""Immutable configuration shared by one gateway application's HTTP consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Request

from base.agents.upload_delivery.paths import (
    MAX_AGENT_UPLOAD_BYTES,
    MAX_AGENT_UPLOAD_FILES,
    MAX_UPLOAD_BYTES,
)
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, Settings
from base.paths import gateway_memory_dir
from gateway.http.middleware.cors import cors_allowed_origins, trusted_request_hosts


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


def gateway_config_from_boot(config: ConfigBoot) -> GatewayConfig:
    """Snapshot HTTP policy without creating or booting another config owner."""
    gateway = config.view.gateway
    services = config.view.services
    gateway_url: str = gateway.gateway_url
    machine = machine_from_boot(config)
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
    return GatewayConfig(
        cluster_secret=config.view.data_plane.cluster_secret,
        session_ttl_seconds=gateway.session_ttl_seconds,
        mcp_endpoint_enabled=gateway.mcp_endpoint_enabled,
        allowed_origins=allowed_origins,
        browser_origin=gateway.browser_origin,
        session_cookie_secure=(
            explicit_secure
            if explicit_secure is not None
            else urlsplit(gateway_url).scheme == "https"
        ),
        memory_root=gateway_memory_dir(roles=machine.role()),
        upload_home=Path.home(),
        human_auth=gateway.human_auth,
        trusted_hosts=trusted_request_hosts(
            allowed_origins=allowed_origins,
            machine_host=machine.host(),
            gateway_url=gateway_url,
        ),
    )


# meiva:note
# The gateway process's own configuration as it reads now.
#
# A handler reads tunables here, never from the process-global `settings`: the gateway's
# config owner is the one thing a test builds the app with (`gateway_test_client(fields=...)`).
def runtime_view(request: Request) -> Settings:
    return request.app.state.gateway_process.config.view
