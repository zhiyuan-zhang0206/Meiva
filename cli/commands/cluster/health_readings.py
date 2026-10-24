# meiva:note
# What one cluster health round observes: the facts `plans.health_verdicts` judges.
#
# Each reader dials, queries or inspects one thing and returns it as a value; none decides
# whether the answer is healthy. `read_health` runs them in the report's order and hands the
# readings over. Everything a round is configured by is read once by the composition root into
# a `HealthOptions`, so a test hands in the gateway's address, a checkout and a data volume of
# its own.

from __future__ import annotations

import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import redis.exceptions

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import gateway_api_base
from base.config import ConfigBoot
from base.deploy.git import source_tree_guard
from base.deploy.release import editable_install
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import (
    CodeBehindSchema,
    SchemaVersionMismatch,
    check_schema_version,
)
from base.events.live.bus import EventBus, event_bus_config_from_config
from base.host.net.http_dial import get as dial_get
from base.paths import root_manifests_path
from cli.commands._repo import roles_or_none
from cli.commands.cluster._provider_guard import ProviderGuardConfig, provider_guard_checks
from cli.commands.cluster.plans.health_verdicts import (
    GatewayReading,
    HealthReadings,
    HealthRules,
    PopulationRead,
    PopulationReading,
    SourceTreeReading,
    liveness_waits,
)
from cli.commands.cluster.plans.service_probe_plan import (
    ROSTER_UNAVAILABLE,
    Launched,
    LaunchRecord,
    LaunchUnreadable,
    NeverLaunched,
    ProbeOutcome,
    RosterGate,
    Unavailable,
    bridge_failure,
    expects_redis_bridge,
    failing_services,
    launch_scope,
    sessions_to_probe,
)
from cli.commands.converge.redis_bridge import probe_redis_bridge
from cli.commands.probe import probe_service
from cli.database.runtime import CommandRuntime
from services.backup.walg import probe as walg_probe
from services.supervision.meiva_root_glue.manifests import launched_services
from services.supervision.roster.spec import services_for_capabilities_annotated

_GATEWAY_TIMEOUT_S = 10.0
_LIVE_AGENTS = (
    "SELECT COUNT(*) FROM agents_meta WHERE status IN ('running', 'idling') "
    "AND liveness_state <> 'offline'"
)


# meiva:note
# What one round is configured by.
#
# `gateway_url` is where the gateway answers; `source_root` is the prod source checkout the
# editable-install and source-tree guards inspect (None when the home has none to guard);
# `data_volume` is the volume whose usage the disk check measures; `checkout_refusal` is why
# this checkout must not drive the probe at all (the 2026-08-07 accident, task #1025: worktree
# code misjudging schema health against prod data), None when it may. The liveness retry
# budget is a tuning pair whose defaults are today's constants.
@dataclass(frozen=True)
class HealthOptions:
    gateway_url: str
    rules: HealthRules
    provider: ProviderGuardConfig
    source_root: Path | None
    editable_allowed_roots: tuple[Path, ...]
    walg_config_file: Path | None
    data_volume: Path
    checkout_refusal: str | None
    liveness_attempts: int = 3
    liveness_retry_interval_s: float = 30.0


# meiva:note
# The gateway's own `/api/health` answers HTTP 200. Connection refused or a timeout means the
# gateway is not answering, which is the unhealthy verdict this probe exists to return.
def gateway_alive(gateway_url: str) -> bool:
    url = f"{gateway_api_base(gateway_url)}/api/health"
    try:
        return dial_get(url, timeout=_GATEWAY_TIMEOUT_S).status_code == 200
    except (httpx.HTTPError, OSError):
        return False


# meiva:note
# True when either dependency behind the gateway is currently unreachable. (redis-py types the
# optional argument of `ping` as Unknown, hence the pyright directive on that call.)
def data_plane_abnormal(config: ConfigBoot, *, database_factory: DatabaseFactory) -> bool:
    try:
        with database_factory().connect(autocommit=True):
            pass
    except (psycopg.Error, OSError):
        return True
    try:
        client = EventBus(event_bus_config_from_config(config)).sync_redis()
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()
    except (redis.exceptions.RedisError, OSError):
        return True
    return False


# meiva:note
# The gateway's liveness after the retry budget. A failed first attempt waits and tries again
# for each wait the budget names; only a gateway that stays dead is followed by a look at the
# data plane behind it, to tell an environment fault from a code fault.
def read_gateway(
    options: HealthOptions, config: ConfigBoot, database_factory: DatabaseFactory
) -> GatewayReading:
    waits = liveness_waits(options.liveness_attempts, options.liveness_retry_interval_s)
    if gateway_alive(options.gateway_url):
        return GatewayReading(alive=True, data_plane_abnormal=False)
    for wait in waits:
        time.sleep(wait)
        if gateway_alive(options.gateway_url):
            return GatewayReading(alive=True, data_plane_abnormal=False)
    return GatewayReading(
        alive=False,
        data_plane_abnormal=data_plane_abnormal(config, database_factory=database_factory),
    )


# meiva:note
# What the last `meiva start` of this home put in its root manifest. A manifest that cannot
# be read is its own reading, never a guess.
def read_launch_record(home: Path) -> LaunchRecord:
    try:
        launched = launched_services(root_manifests_path(home))
    except (OSError, ValueError) as exc:
        return LaunchUnreadable(str(exc))
    return NeverLaunched() if launched is None else Launched(launched)


# meiva:note
# Count the agents running or idling on machines not judged offline, straight from the central
# DB: the probe runs on the gateway machine and has DB access. A cluster with zero live agents
# is effectively dead.
def read_population(database_factory: DatabaseFactory, launched: LaunchRecord) -> PopulationReading:
    try:
        with database_factory().connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(_LIVE_AGENTS)
            row = cur.fetchone()
    except (psycopg.Error, OSError):
        return PopulationReading(PopulationRead.DB_UNREACHABLE, 0, launched)
    if row is None:
        return PopulationReading(PopulationRead.NO_ROW, 0, launched)
    return PopulationReading(PopulationRead.COUNTED, int(row[0]), launched)


# meiva:note
# Whether the applied schema disagrees with this checkout's required migrations, reusing the
# `check_schema_version` invariant every daemon runs at start (CodeBehindSchema: code lacks
# applied migrations; SchemaVersionMismatch: the DB lacks the code's). A DB that cannot be
# reached is NOT a skew: the connection is the precondition for reading the applied set, and
# a transient pgbouncer blip must not fire a false schema alert while code and DB are actually
# in sync (2026-08-03: the probe alerted "applied version behind required" on a connection
# error during a pgbouncer flake).
def read_schema_skew(database_factory: DatabaseFactory) -> bool:
    try:
        with database_factory().connect(autocommit=True) as conn:
            check_schema_version(conn, migrations_dir=MIGRATIONS_DIR)
    except (CodeBehindSchema, SchemaVersionMismatch):
        return True
    except (psycopg.Error, OSError):
        return False
    return False


# meiva:note
# Probe every service this host's roles should be running and return the failing ones. The
# gateway's Redis relay rides along: it is a gateway-host listener with the same alert-only
# class as a dead service.
def read_service_failures(command: CommandRuntime, launched: LaunchRecord) -> tuple[str, ...]:
    roles = roles_or_none(machine=command.machine)
    if roles is None:
        return (ROSTER_UNAVAILABLE,)
    scope = launch_scope(launched)
    if isinstance(scope, Unavailable):
        return (scope.reason,)
    roster = services_for_capabilities_annotated(roles, machine=command.machine, host=command.host)
    wanted = sessions_to_probe(scope, tuple(RosterGate(s.session, why) for s, why in roster))
    outcomes: list[ProbeOutcome] = []
    for spec in (spec for spec, _why in roster if spec.session in wanted):
        probe = probe_service(spec)
        outcomes.append(ProbeOutcome(spec.session, probe.alive, probe.detail))
    failing = failing_services(outcomes)
    if not expects_redis_bridge(roles):
        return failing
    status = probe_redis_bridge(command.home, machine=command.machine, config=command.config)
    bridge = bridge_failure(
        required=status.required,
        serving=status.serving,
        endpoint=status.endpoint,
        detail=status.detail,
    )
    return failing if bridge is None else (*failing, bridge)


# meiva:note
# Used fraction of the data volume, statvfs-family, or None when unmeasurable.
#
# Uses `shutil.disk_usage`, the same measurement the trace disk-watermark guard
# (`MEIVA_TRACE_DISK_WATERMARK`, `base/telemetry/trace_mirror.py`) and the macmini resource
# watcher make, so the probe fires at the line the owner is already told about. df(1) was the
# original measure, but its offset to the statvfs family is unstable in both directions
# (2026-08-24: ~0.8 points higher; 2026-09-14: ~2-4 points lower), and a df-calibrated 0.90
# fires at about statvfs 0.938, later than the trace auto-degrade this alert exists to
# precede (metric canon 2026-09-14: judge disk waterlines in the statvfs family only).
def disk_usage_fraction(volume: Path) -> float | None:
    try:
        usage = shutil.disk_usage(volume)
    except OSError:
        return None
    return usage.used / usage.total


# meiva:note
# The first broken WAL-archiving condition, or None (also None while WAL-G is off), and whether
# WAL-G is on. The path is the configuration's WAL-G config file, read once by the root.
def read_walg(config_file: Path | None) -> tuple[str | None, bool]:
    return (
        walg_probe.failure(path=config_file),
        config_file is not None,
    )


# meiva:note
# Editable-install records of the prod venv that name source outside the allowlist.
#
# A polluted virtualenv can point at disposable source even when the checkout itself is
# unchanged. This reads only; ordinary start and converge do not repair editable installs.
# The shared inspection applies exact-root allowlisting (production source plus the stable
# ~/Meiva clone), never an arbitrary descendant.
def read_editable_violations(
    source_root: Path | None, allowed_roots: Iterable[Path]
) -> tuple[str, ...]:
    if source_root is None:
        return ()
    return (
        *editable_install.editable_install_violations(source_root, allowed_roots=allowed_roots),
        *editable_install.editable_console_script_violations(source_root),
    )


# meiva:note
# Source tamper or unavailable inspection of a home that executes its source checkout, without
# repairing files. The guard marks "I could not look" with a prefix on its findings; this
# reading carries that as a flag so the wording is decided from values.
def read_source_tree(source_root: Path | None) -> SourceTreeReading:
    if source_root is None:
        return SourceTreeReading(guard_skipped=False, findings=())
    violations = source_tree_guard.source_tree_violations(source_root)
    prefix = source_tree_guard.GUARD_SKIPPED_PREFIX
    if any(v.startswith(prefix) for v in violations):
        return SourceTreeReading(
            guard_skipped=True, findings=tuple(v.removeprefix(prefix).strip() for v in violations)
        )
    return SourceTreeReading(guard_skipped=False, findings=violations)


# meiva:note
# Observe the whole cluster once, in the report's order.
def read_health(
    options: HealthOptions, *, database_factory: DatabaseFactory, command: CommandRuntime
) -> HealthReadings:
    launched = read_launch_record(command.home)
    walg_failure, walg_on = read_walg(options.walg_config_file)
    return HealthReadings(
        disk_fraction=disk_usage_fraction(options.data_volume),
        gateway=read_gateway(options, command.config, database_factory),
        population=read_population(database_factory, launched),
        schema_skew=options.rules.check_schema and read_schema_skew(database_factory),
        service_failures=read_service_failures(command, launched),
        walg_failure=walg_failure,
        walg_enabled=walg_on,
        editable_violations=read_editable_violations(
            options.source_root, options.editable_allowed_roots
        ),
        source_tree=read_source_tree(options.source_root),
        provider=provider_guard_checks(options.provider, database_factory=database_factory),
    )
