"""Service spec — capability filtering and gate policy for machine desired state.

Given a host's capability set, this K8s-shaped module family answers what should
run. ``services.supervision.roster.build_services()`` is the canonical roster; this module applies
capability selection and runtime gates to it.

Every service declares its ``ServiceSpec.capabilities`` in one of three groups:
gateway-only, agent-runner-only, or both. ``services_for_capabilities(roles)``
selects services whose capabilities intersect the host's roles. A service also
declares ``requires_db`` so database-dependent readiness remains explicit.

Plugins expose ``services() -> tuple[ServiceSpec, ...]`` from their services
module. ``plugin_services()`` discovers code-present plugins and appends them
to the roster: plugin declares, the roster discovers. Each plugin service's
own ``ServiceSpec.gate`` keeps cluster-level enablement out of ``gate_reason``.
The fleet task daemon follows this path; see
``docs/decisions/extensions/plugins/2026-07-19-plugin-registered-services.md``.

Layer: this module family lives in ``services/supervision``; it imports ``base``,
plus lazy function-local reaches into the shared-tier browser identity probe and
gate app-port source. Nothing reaches up into cli/gateway, so start, root
monitoring, and ``meiva status`` share one roster.

Native Postgres, Redis, and PgBouncer have separate data-plane custody so they
can remain available during an application-root transition. The macOS helper
is root's platform parent, not an application service. Read-only extra checks
live in the root diagnostic roster; they never acquire service ownership.
The definitions live in ``services.supervision.roster.service_spec``,
``services.supervision.roster``, and ``services.supervision.roster.spec``;
callers import them from there.
"""

from __future__ import annotations

from typing import assert_never

from base.cluster.dataplane.redis_bridge import RedisBridgeUnconfiguredError, redis_bridge_endpoint
from base.cluster.machine import MachineIdentity, MachineRoles
from base.config import settings
from base.host.env.registry import process_environment
from base.host.system.hostport import Host
from base.host.system.probes import (
    browser_incapability,
    permissions_helper_incapability,
)
from base.log import logger
from base.paths import meiva_home
from base.telemetry.observability import (
    collector_allowed_for_home,
    endpoint_override_is_explicit,
    gateway_observability_home,
)
from services.supervision.roster import build_services
from services.supervision.roster.gate_plan import (
    BridgeFact,
    BrowserFact,
    CollectorFact,
    ComputerFact,
    FlagFact,
    GateFact,
    GateKind,
    StationFact,
    gate_kind,
)
from services.supervision.roster.gate_plan import gate_reason as gate_reason_of
from services.supervision.roster.plugin_services import PluginServiceError
from services.supervision.roster.service_spec import (
    ServiceSpec as ServiceSpec,  # re-export: generated plugin fixtures
)


# meiva:note
# The one fact the gate of a core session reads, collected from the world: the settings
# flag, the platform probe or the marker file the session's kind names. `gate_plan` judges it.
def _gate_fact(
    kind: GateKind, session: str, *, machine: MachineIdentity, host: Host
) -> GateFact | None:
    match kind:
        case "flag":
            return FlagFact(enabled=_flag_enabled(session))
        case "browser":
            enabled = settings.services.browser_enabled
            return BrowserFact(
                enabled=enabled,
                incapability=(
                    browser_incapability(settings.services.chrome_binary, host=host)
                    if enabled
                    else None
                ),
            )
        case "computer-mcp":
            helper_enabled = settings.services.permissions_helper_enabled
            return ComputerFact(
                helper_enabled=helper_enabled,
                incapability=(
                    permissions_helper_incapability(host=host) if helper_enabled else None
                ),
            )
        case "station":
            from base.cluster.machine import MachineRoleMissing
            from services.supervision.healthchecks.lgtm import is_lgtm_host

            try:
                roles = machine.role()
            except MachineRoleMissing:
                roles = None
            return StationFact(is_station=is_lgtm_host(meiva_home(), roles=roles))
        case "collector":
            return CollectorFact(allowed=_collector_allowed(machine))
        case "bridge":
            return _bridge_fact(machine)
        case "none":
            return None
        case _:
            assert_never(kind)


# meiva:note
# The settings flag of a session whose only gate is one flag.
def _flag_enabled(session: str) -> bool:
    match session:
        case "heartbeat":
            return settings.daemon.heartbeat_enabled
        case "delivery-watchdog":
            return settings.daemon.delivery_watchdog_enabled
        case "im-bridge":
            return settings.services.im_bridge_enabled
        case _:
            raise ValueError(f"session {session!r} has no settings-flag gate")


# meiva:note
# This is the roster sibling of `ensure_otel_collector_step` in
# `cli/commands/observability/otel_collector.py` and `_collector_serves_this_home` in
# `services/supervision/healthchecks/otel_collector.py`. All three share
# `collector_allowed_for_home` (marker OR station capability OR explicit
# `MEIVA_TELEMETRY_OTLP_ENDPOINT` override) so the roster, `meiva start`, `meiva status`, the
# root, rollout readiness and the cluster health probe agree about which gateway owns the
# collector. Pure agent-runners retain their relay collector.
def _collector_allowed(machine: MachineIdentity) -> bool:
    home = meiva_home()
    return collector_allowed_for_home(
        gateway_observability_home(machine, home=home),
        machine=machine,
        unit_home=home,
        endpoint_explicit=endpoint_override_is_explicit(
            "MEIVA_TELEMETRY_OTLP_ENDPOINT", process_environment()
        ),
    )


# meiva:note
# The relay exists only on a macOS gateway with a local data plane and a declared non-loopback
# address; the same answer drives `meiva status` and the cluster health probe.
def _bridge_fact(machine: MachineIdentity) -> BridgeFact:
    try:
        endpoint = redis_bridge_endpoint(
            meiva_home(),
            machine_host=machine.host,
            remote_data_plane=lambda: settings.data_plane.is_remote,
        )
    except RedisBridgeUnconfiguredError as exc:
        return BridgeFact(unconfigured=str(exc), has_endpoint=False)
    return BridgeFact(unconfigured=None, has_endpoint=endpoint is not None)


def _plugin_gate_reason(spec: ServiceSpec) -> str | None:
    """A plugin gate must supply its declared reason contract."""
    if spec.gate is None:
        raise PluginServiceError(f"service {spec.session!r} has no plugin gate")
    reason = spec.gate()
    if reason is not None and not isinstance(reason, str):
        raise PluginServiceError(f"gate for {spec.session!r} must return str or None")
    return reason


def gate_reason(spec: ServiceSpec, *, machine: MachineIdentity, host: Host) -> str | None:
    """Why a service is config/capability-gated OUT of the start roster, or None if
    it will run. The single place the gate's *reason* is computed, so the start
    path (drops it), ``meiva start`` (logs it), and ``meiva status`` (shows it) stay
    consistent.

    A service that carries its own ``gate`` (plugin-registered services) is asked
    directly — its fleet/plugin-domain toggle lives with the plugin, not here.
    Core services are gated by session name in `_core_gate_reason`.
    """
    if spec.gate is not None:
        try:
            return _plugin_gate_reason(spec)
        except Exception:
            logger.opt(exception=True).error(
                "gate for {} failed; roster evaluation aborted", spec.session
            )
            raise
    kind = gate_kind(spec.session)
    return gate_reason_of(spec.session, _gate_fact(kind, spec.session, machine=machine, host=host))


def services_for_capabilities_annotated(
    roles: MachineRoles, *, machine: MachineIdentity, host: Host
) -> tuple[tuple[ServiceSpec, str | None], ...]:
    """The union of services this host's capability set runs, each paired with its
    gate reason (None = will start, a string = gated out + why).

    A service is included iff its ``capabilities`` intersect ``roles`` — so a
    gateway host gets the gateway-group services, an agent-runner host the
    agent-runner-group, and a single box the union. Iterates ``build_services()``
    in authored order, so each single-capability view keeps its load-bearing order.

    Config/capability-gated services (browser, disabled heartbeat; and
    plugin services with their own gate, e.g. disabled task-maintenance) are kept
    in this list WITH their reason rather than dropped, so ``meiva start`` /
    ``meiva status`` can show WHY a service is absent instead of silently shrinking
    the roster. ``services_for_capabilities`` is the start-roster view that drops
    them.
    """
    return tuple(
        (s, gate_reason(s, machine=machine, host=host))
        for s in build_services()
        if s.capabilities & roles
    )


def services_for_capabilities(
    roles: MachineRoles, *, machine: MachineIdentity, host: Host
) -> tuple[ServiceSpec, ...]:
    """The start roster: the capability-union services that will actually launch
    (config/capability-gated ones dropped). ``services_for_capabilities_annotated``
    is the diagnostic view that keeps the gated-out services + their reason."""
    return tuple(
        s
        for s, reason in services_for_capabilities_annotated(roles, machine=machine, host=host)
        if reason is None
    )
