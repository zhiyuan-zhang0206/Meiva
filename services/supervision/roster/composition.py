# meiva:note
# Assemble the canonical application roster at use-time. Declaration consumers use
# the light package door; full roster callers load its service-specific observers.
from __future__ import annotations

import shlex
from collections.abc import Mapping
from functools import partial
from pathlib import Path

from base.cluster import frontend_service_cmd
from base.cluster.dataplane.redis_bridge import (
    RedisBridgeEndpoint,
    RedisBridgeUnconfiguredError,
    redis_bridge_endpoint,
)
from base.config import get_field
from base.daemon.health import DaemonProbe
from base.host.net.url_secret import names_foreign_host
from base.native_process.stop_timing import HOST_STOP_CEILING_S, OPS_STOP_CEILING_S
from base.paths import meiva_home, otel_collector_binary, otel_collector_config
from services.supervision.roster import plugin_services
from services.supervision.roster.healthz import healthz_daemon
from services.supervision.roster.service_spec import AGENT_RUNNER, BOTH, GATEWAY, ServiceSpec

# meiva:note
# After uvicorn drains connections, gateway lifespan cleanup still joins admitted
# work and closes its pools before the interpreter exits. Root must allow that tail.
_GATEWAY_LIFESPAN_ALLOWANCE_S = 10.0


# meiva:note
# Render the observed relay endpoint as command, listen address and port. No
# endpoint keeps the bare declaration for a host whose gate excludes the relay.
def redis_bridge_launch(endpoint: RedisBridgeEndpoint | None) -> tuple[str, str, int]:
    relay = "services/redis_bridge/relay.py"
    if endpoint is None:
        return f"/usr/bin/python3 {relay}", "", 0
    argv = [
        "/usr/bin/python3",
        relay,
        "--listen-host",
        endpoint.listen_host,
        "--listen-port",
        str(endpoint.port),
        "--backend-port",
        str(endpoint.port),
    ]
    return shlex.join(argv), endpoint.listen_host, endpoint.port


# meiva:note
# Observe the relay endpoint when the roster is built. The required-relay gate
# reports an unconfigured home; its declaration still carries the bare command.
def redis_bridge_command(home: Path) -> tuple[str, str, int]:
    try:
        endpoint = redis_bridge_endpoint(
            home,
            machine_host=lambda: str(get_field("machine_host")).strip() or "localhost",
            remote_data_plane=lambda: names_foreign_host(
                str(get_field("db_url")), str(get_field("redis_url"))
            ),
        )
    except RedisBridgeUnconfiguredError:
        endpoint = None
    return redis_bridge_launch(endpoint)


# meiva:note
# Import the frontend's application HTTP observer only when its probe runs; `port` is the
# application port the roster resolved when it was built.
def frontend_probe(port: int) -> DaemonProbe:
    from services.supervision.healthchecks.frontend import probe_frontend_at

    return probe_frontend_at(port)


# meiva:note
# Import the headed browser's profile-and-listener observer only when it runs.
# The browser domain owns those identity facts; the roster does not restate them. `port` is
# the CDP port and the Chrome profile directory the roster resolved when it was built.
def browser_probe(port: int, profile: Path) -> DaemonProbe:
    from services.supervision.healthchecks.browser import browser_probe as observe

    return observe(port, profile)


# meiva:note
# Capture ports, URLs and memory-search inputs at roster construction. Root builds
# this roster once at startup; persisted config changes require process restart.
# The gateway, agent-runner and shared groups retain their concatenation order.
def build_services(
    *,
    plugins: tuple[ServiceSpec, ...] | None = None,
    plugin_dirs: Mapping[str, Path] | None = None,
) -> tuple[ServiceSpec, ...]:

    from services.agent_runner.exec_host import shutdown_budget as exec_host_budget
    from services.agent_runner.pty_sessions import shutdown_budget as pty_sessions_budget
    from services.desktop.browser import shutdown_budget as browser_mcp_budget
    from services.desktop.browser.profile import profile_dir
    from services.entrypoints.gate.daemon import app_port, entry_port
    from services.supervision.healthchecks.gate import probe as probe_gate
    from services.supervision.healthchecks.gateway import gateway_probe_at
    from services.supervision.healthchecks.memory_search import memory_search_probe
    from services.supervision.healthchecks.otel_collector import probe_collector_at
    from services.supervision.healthchecks.protocol_probe import probe as probe_protocol
    from services.supervision.healthchecks.redis_bridge import probe_relay

    home = meiva_home()
    _fe_port = app_port()
    _fe_url = f"http://localhost:{_fe_port}"
    _bridge_cmd, _bridge_host, _bridge_port = redis_bridge_command(home)
    gateway_health_url = str(get_field("gateway_health_url"))
    gateway_shutdown_timeout_s = float(get_field("gateway_graceful_shutdown_timeout_seconds"))
    memory_search_port = int(get_field("memory_search_port"))
    memory_search_uri = str(get_field("memory_search_uri"))
    embedding_backend = str(get_field("embedding_backend"))
    gateway_port = int(get_field("gateway_port"))
    browser_origin = str(get_field("browser_origin"))
    browser_cdp_port = int(get_field("browser_cdp_port"))
    browser_profile = profile_dir(home)
    telemetry_otlp_port = int(get_field("telemetry_otlp_port"))
    probe_protocol_service = partial(probe_protocol, home=home, browser_cdp_port=browser_cdp_port)

    gateway_services = (
        ServiceSpec(
            session="gate",
            cmd=".venv/bin/python -m services.entrypoints.gate.daemon",
            capabilities=GATEWAY,
            requires_db=False,
            curl_url=f"http://127.0.0.1:{entry_port()}/__meiva/healthz",
            identity_probe=probe_gate,
            healthcheck_module="services.supervision.healthchecks.gate",
            # meiva:note
            # The gate names its home in /__meiva/healthz, so a foreign gate occupying the
            # entry port can be identified before this unit launches.
            home_healthz=True,
        ),
        ServiceSpec(
            session="gateway",
            cmd=".venv/bin/python -m gateway",
            capabilities=GATEWAY,
            requires_db=True,
            # meiva:note
            # Gateway reload can fork a serving worker whose pid differs from the recorded
            # pidfile. Bind readiness to the answering home's HTTP identity instead.
            curl_url=gateway_health_url,
            identity_probe=partial(gateway_probe_at, gateway_health_url),
            healthcheck_module="services.supervision.healthchecks.gateway",
            # meiva:note
            # Uvicorn drains requests before lifespan cleanup. Root's stop window must
            # cover both serving shutdown and the application's admitted work.
            stop_ceiling_s=(gateway_shutdown_timeout_s + _GATEWAY_LIFESPAN_ALLOWANCE_S),
        ),
        healthz_daemon(
            "im-bridge",
            module="services.entrypoints.im_bridge.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        healthz_daemon(
            "labeler",
            module="services.derived.labeler.daemon",
            capabilities=GATEWAY,
            requires_db=True,
            # meiva:note
            # Label generation builds chat models and needs the LLM provider keys. A
            # gateway profile would remove those keys; this daemon boots with full Settings.
            no_profile_marker=True,
        ),
        healthz_daemon(
            "heartbeat",
            module="services.wake.heartbeat.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        # meiva:note
        # The cluster-wide stale-inbound tripwire belongs to the gateway data plane;
        # its service gate honors MEIVA_DELIVERY_WATCHDOG_ENABLED.
        healthz_daemon(
            "delivery-watchdog",
            module="services.wake.delivery_watchdog.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        # meiva:note
        # Checkpoint reaping and blob vacuum are unconditional gateway maintenance.
        # Gating this unit would let checkpoint_blobs grow without bound.
        healthz_daemon(
            "events-maintenance",
            module="services.upkeep.events_maintenance.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        # meiva:note
        # The gateway owns deadline state and asks runners to kill expired sessions;
        # each runner must not independently reap the same cluster records.
        healthz_daemon(
            "ttl-reaper",
            module="services.upkeep.ttl_reaper.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        # meiva:note
        # Scheduling reads cluster rows but launches sessions from this gateway's own
        # checkout. The daemon refuses to run from another checkout.
        healthz_daemon(
            "schedule-manager",
            module="services.wake.schedule_manager.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        ServiceSpec(
            session="insights",
            cmd=".venv/bin/python -m services.derived.insights.daemon",
            capabilities=GATEWAY,
            requires_db=True,
            # meiva:note
            # Insights serves its gateway caller over a Unix socket, so readiness has no
            # TCP port slot and uses the service protocol identity.
            identity_probe=partial(probe_protocol_service, "insights"),
            healthcheck_module="services.supervision.healthchecks.insights",
        ),
        # meiva:note
        # The memory indexer connects to its selected backend at cold start. Declare
        # memory-search first so storage is ready before plugin-contributed indexing.
        ServiceSpec(
            session="memory-search",
            cmd=".venv/bin/python -m services.derived.memory_search.daemon",
            capabilities=GATEWAY,
            requires_db=False,
            # meiva:note
            # The numpy backend keeps an in-memory matrix and npz store. A Postgres
            # outage must not block the memory-search service that uses that backend.
            tcp_port=memory_search_port,
            identity_probe=partial(
                memory_search_probe,
                uri=memory_search_uri,
                embedding_name=embedding_backend,
            ),
            healthcheck_module="services.supervision.healthchecks.memory_search",
        ),
        ServiceSpec(
            session="frontend",
            # meiva:note
            # The frontend command has one owner, including build-time NEXT_PUBLIC values
            # and exec handover. Derive it here rather than duplicating a launch string.
            cmd=frontend_service_cmd(
                _fe_port,
                gateway_port=gateway_port,
                browser_origin=browser_origin,
            ),
            capabilities=GATEWAY,
            requires_db=False,
            # meiva:note
            # Next.js reaches data through the gateway HTTP API. It can remain up during
            # a database outage to show that outage to the operator.
            curl_url=_fe_url,
            identity_probe=partial(frontend_probe, _fe_port),
            healthcheck_module="services.supervision.healthchecks.frontend",
        ),
        healthz_daemon(
            "pg-backup",
            module="services.backup.scheduler.daemon",
            capabilities=GATEWAY,
            requires_db=True,
        ),
        # meiva:note
        # A macOS gateway relays off-box Redis ingress while Redis stays loopback-only.
        # The system interpreter is the firewall-recognized serving binary. The host
        # gate excludes unnecessary relays; failing listeners use root restart limits.
        ServiceSpec(
            session="redis-bridge",
            cmd=_bridge_cmd,
            capabilities=GATEWAY,
            requires_db=False,
            identity_probe=partial(probe_relay, _bridge_host, _bridge_port),
            healthcheck_module="services.supervision.healthchecks.redis_bridge",
        ),
    )

    agent_runner_services = (
        # meiva:note
        # One runner owns page-server processes for the agent_pages rows assigned to
        # that host; the database, rather than a process census, is its desired state.
        healthz_daemon(
            "page-server",
            module="services.agent_runner.page_server.daemon",
            capabilities=AGENT_RUNNER,
            requires_db=True,
        ),
        healthz_daemon(
            "agent-host",
            module="services.agent_runner.agent_host.daemon",
            capabilities=AGENT_RUNNER,
            # meiva:note
            # The agent host runs the kernel in-process. A runner profile removes the
            # agent settings it needs, so this unit requires an explicit agent profile.
            profile="agent",
            requires_db=True,
            stop_ceiling_s=HOST_STOP_CEILING_S,
        ),
        ServiceSpec(
            session="exec-host",
            cmd=".venv/bin/python -m services.agent_runner.exec_host.daemon",
            capabilities=AGENT_RUNNER,
            profile="agent",
            requires_db=False,
            db_access="runner",
            identity_probe=partial(probe_protocol_service, "exec-host"),
            stop_ceiling_s=exec_host_budget.SHUTDOWN_CEILING_S,
        ),
        healthz_daemon(
            "ops",
            module="services.agent_runner.agent_ops.daemon",
            capabilities=AGENT_RUNNER,
            requires_db=True,
            # meiva:note
            # SIGTERM drains requests agent_ops.in_flight already admitted before closing.
            # Root must wait for that same bounded drain budget.
            stop_ceiling_s=OPS_STOP_CEILING_S,
        ),
        # meiva:note
        # The capability/config gate decides whether this host can run headed Chrome.
        # Readiness still needs the browser domain to prove profile and listener identity;
        # a CDP HTTP response alone does not establish ownership.
        ServiceSpec(
            session="browser",
            cmd=".venv/bin/python -m services.desktop.browser.daemon",
            capabilities=AGENT_RUNNER,
            requires_db=False,
            # meiva:note
            # Headed Chrome has no database dependency. Root must keep reviving it during
            # a database outage, when the browser may be needed for recovery.
            curl_url=f"http://127.0.0.1:{browser_cdp_port}/json/version",
            identity_probe=partial(browser_probe, browser_cdp_port, browser_profile),
            healthcheck_module="services.supervision.healthchecks.browser",
        ),
        # meiva:note
        # This machine-local multiplexer owns a Unix socket plus CDP, with no database.
        # Its protocol observer probes that socket rather than an HTTP/TCP substitute.
        ServiceSpec(
            session="browser-mcp",
            cmd=".venv/bin/python -m services.desktop.browser.mcp_daemon",
            capabilities=AGENT_RUNNER,
            requires_db=False,
            identity_probe=partial(probe_protocol_service, "browser-mcp"),
            healthcheck_module="services.supervision.healthchecks.browser_mcp",
            stop_ceiling_s=browser_mcp_budget.SHUTDOWN_CEILING_S,
        ),
        # meiva:note
        # Desktop actions run through the signed permissions helper and write database
        # audit events. The host gate requires native capability before starting it.
        ServiceSpec(
            session="computer-mcp",
            cmd=".venv/bin/python -m services.desktop.computer.mcp_daemon",
            capabilities=AGENT_RUNNER,
            requires_db=True,
            identity_probe=partial(probe_protocol_service, "computer-mcp"),
            healthcheck_module="services.supervision.healthchecks.computer_mcp",
        ),
        # meiva:note
        # One local MCP daemon multiplexes isolated client sessions and stdio children.
        # Its configuration is local mcp.json; boot and runtime need no database.
        ServiceSpec(
            session="mcp-daemon",
            cmd=".venv/bin/python -m meiva.mcps._daemon",
            capabilities=AGENT_RUNNER,
            requires_db=False,
            identity_probe=partial(probe_protocol_service, "mcp-daemon"),
            healthcheck_module="services.supervision.healthchecks.mcp_daemon",
        ),
    )

    both_services: tuple[ServiceSpec, ...] = (
        # meiva:note
        # A shell session outlives its agent, agent host and gateway. This local PTY
        # owner keeps sessions in a socket and ledger, independent of the database;
        # explicit stop closes sessions and writes their owners notices.
        ServiceSpec(
            session="pty-sessions",
            cmd=".venv/bin/python -m services.agent_runner.pty_sessions.daemon",
            capabilities=BOTH,
            requires_db=False,
            identity_probe=partial(probe_protocol_service, "pty-sessions"),
            stop_ceiling_s=pty_sessions_budget.SHUTDOWN_CEILING_S,
        ),
        # meiva:note
        # Each machine sends traces to its local sidecar. Gateway collectors reach
        # loopback backends; runner collectors relay to authenticated private ingress.
        # Bounded queues buffer or shed during an outage without a database dependency.
        ServiceSpec(
            session="otel-collector",
            cmd=shlex.join(
                [
                    str(otel_collector_binary(home)),
                    "--config",
                    str(otel_collector_config(home)),
                ]
            ),
            config_inputs=(otel_collector_config(home),),
            capabilities=BOTH,
            requires_db=False,
            # meiva:note
            # The observer posts a valid empty OTLP ExportTraceServiceRequest: a listening
            # TCP port alone does not prove the collector protocol is ready.
            tcp_port=telemetry_otlp_port,
            identity_probe=partial(probe_collector_at, telemetry_otlp_port),
            healthcheck_module="services.supervision.healthchecks.otel_collector",
        ),
    )

    from base.telemetry.lgtm_local import (
        BACKENDS,
        HEALTH_PATHS,
        backend_urls,
        service_argv,
        service_input_paths,
    )
    from services.supervision.healthchecks.lgtm import probe_backend

    urls = backend_urls()
    observability_services = tuple(
        ServiceSpec(
            session=name,
            cmd=shlex.join(service_argv(home, name)),
            config_inputs=service_input_paths(home, name),
            capabilities=frozenset({"gateway", "agent-runner", "observability-station"}),
            requires_db=False,
            curl_url=urls[name] + HEALTH_PATHS[name],
            identity_probe=partial(probe_backend, name, urls=urls),
            healthcheck_module="services.supervision.healthchecks.lgtm",
        )
        for name in BACKENDS
    )
    core = gateway_services + agent_runner_services + both_services + observability_services

    # meiva:note
    # Append plugin declarations after core groups. Session-name collisions fail
    # fast because root supervision and status derive their keys from the session.
    plugin = (
        plugin_services.plugin_services(plugin_dirs=plugin_dirs) if plugins is None else plugins
    )
    plugin_services.assert_unique_sessions(core, plugin)
    return core + plugin
