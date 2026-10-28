"""Deployment diagnostics: protocol observation and alerting.

Endpoint configuration and authentication follow their existing owners. No adapter calls
an ensure, start, stop, launchd repair, or session command.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from base.agents.impersonation import live_lease_on_machine
from base.clock import Clock
from base.cluster.machine import MachineIdentityReader
from base.cluster.record import ClusterRecord
from base.daemon.health import DaemonProbe
from base.db import Database
from base.host import brew_pin, proc
from base.host.db_authority.api import read_telemetry_token
from base.host.net.ports import SYSTEM_URL_OPENER, UrlOpener
from base.host.net.url_secret import names_foreign_host
from base.host.proc import CommandRunner
from base.native_process.ownership import OwnedProcess
from base.telemetry.station_endpoint import StationEndpointConfig
from services.desktop.browser.probe import probe_browser as _default_probe_browser
from services.desktop.permissions_helper.launchd_job import LaunchdFacts
from services.supervision.healthchecks.browser_reach import (
    CanaryResult,
    HostResult,
)
from services.supervision.healthchecks.browser_reach import (
    canary as _default_canary,
)
from services.supervision.healthchecks.browser_reach import (
    host_probe as _default_host_probe,
)
from services.supervision.healthchecks.redis_acl import (
    RedisPingConnection,
)
from services.supervision.healthchecks.redis_acl import (
    ping as _default_redis_ping,
)
from services.supervision.meiva_root.health import LeaseProbe
from services.supervision.meiva_root_glue.diagnostics import DIAGNOSTIC_TIMEOUT_S, Diagnostic


# meiva:note
# The settings the host diagnostics decide on, read once by the root that builds them
# (`glue.build_wiring`) and frozen: a probe never reads configuration while it runs. `loki_url` is
# the local Loki base URL with a wildcard bind already resolved to loopback. The two URLs and the
# environment can carry credentials, so they stay out of the repr.
@dataclass(frozen=True)
class ProbeConfig:
    home: Path
    helper_socket: Path
    redis_url: str = field(repr=False)
    db_url: str = field(repr=False)
    pgbouncer_enabled: bool
    observability_url: str
    observability_otlp_port: int
    gateway_health_url: str
    browser_cdp_port: int
    browser_profile: Path
    browser_reach_timeout_s: float
    browser_reach_probe_interval_s: float
    loki_url: str
    environment: Mapping[str, str] = field(repr=False)


# meiva:note
# The whole time one host check may spend — one deadline for the check, never a per-command literal.
# Each subcommand may spend an even share of what the legs before it left, so a hung leg cannot
# outlive the check and the legs after it still run and report what they find. The value comes from
# what the checks measure on a quiet host (brew query 0.7s, venv legs 0.9-1.0s) against what a
# loaded one costs: the venv import smoke alone imports Meiva, Pydantic, Psycopg and FastAPI, so 5
# seconds of it was already tight on a busy Mac (2026-10-27, task #5656).
PROBE_DEADLINE_S: float = 18.0


# meiva:note
# `deadline_s` is the whole deadline the diagnostic gives this check, never a per-command literal,
# and each query may spend an even share of what the queries before it left — the same rule the venv
# legs follow, so one hung query cannot hide the answer the other one still has: its finding is
# reported when it finds one, and only the queries that never answered make the check inconclusive.
#
# `HOMEBREW_NO_AUTO_UPDATE` is pinned in the child's environment because brew updates its own
# formulae on demand otherwise, which takes tens of seconds and is not what this check measures:
# with it pinned, the sample measures the pin set rather than brew's maintenance.
def brew_pins(
    *,
    environment: Mapping[str, str],
    brew_binary: str = "brew",
    deadline_s: float = PROBE_DEADLINE_S,
) -> DaemonProbe:
    deadline = time.monotonic() + deadline_s
    options = ("--pinned", "--formula")
    observed: list[set[str]] = []
    unmeasured: list[str] = []
    for index, option in enumerate(options):
        share = (deadline - time.monotonic()) / (len(options) - index)
        if share <= 0:
            unmeasured.append(
                f"brew list {option} was never started: the {deadline_s:g}s observation deadline "
                f"was already spent"
            )
            continue
        try:
            result = proc.run_bounded(
                [brew_binary, "list", option],
                timeout=share,
                capture_output=True,
                text=True,
                env={**environment, "HOMEBREW_NO_AUTO_UPDATE": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            unmeasured.append(
                f"brew list {option} did not finish within its share of the {deadline_s:g}s "
                f"observation deadline; stderr={exc.stderr!r}"
            )
            continue
        result.check_returncode()
        observed.append(set(result.stdout.splitlines()))
    if unmeasured:
        return DaemonProbe.inconclusive("; ".join(unmeasured))
    pinned, installed = observed
    missing = sorted((brew_pin.PINNED_BREW_FORMULAE & installed) - pinned)
    if missing:
        return DaemonProbe.down(f"approved Homebrew formulae are unpinned: {', '.join(missing)}")
    return DaemonProbe.up("installed approved Homebrew formulae are pinned")


# meiva:note
# The checkout this process's root is actually executing. Another cluster's production checkout is
# a different environment and is never the subject of this observation; a caller that inspects a
# different tree says so.
_ROOT_CHECKOUT = Path(__file__).resolve().parents[3]


# meiva:note
# `deadline_s` is the whole deadline, as in `brew_pins`; the legs share it.
def venv(
    *,
    environment: Mapping[str, str],
    run: CommandRunner,
    clock: Clock,
    deadline_s: float = PROBE_DEADLINE_S,
    source_root: Path = _ROOT_CHECKOUT,
) -> DaemonProbe:
    from services.supervision.healthchecks import prod_venv

    uv = shutil.which("uv", path=environment.get("PATH", ""))
    if uv is None:
        return DaemonProbe.unavailable("uv unavailable: dependency consistency was not measured")
    observation = prod_venv.observe_venv(
        source_root=source_root,
        uv=uv,
        environment=environment,
        deadline_s=deadline_s,
        run=run,
        clock=clock,
    )
    if observation.violations:
        return DaemonProbe.down("; ".join(observation.violations))
    if observation.unmeasured:
        return DaemonProbe.inconclusive(
            f"no verdict within the {deadline_s:g}s observation deadline: "
            f"{'; '.join(observation.unmeasured)}"
        )
    return DaemonProbe.up("root checkout dependency and import checks passed")


def redis_acl(
    *,
    redis_url: str,
    connection: RedisPingConnection | None = None,
) -> DaemonProbe:
    from base.cluster import ownership

    port = ownership.redis_port_of(redis_url)
    if port is None:
        return DaemonProbe.unavailable("no explicit local Redis endpoint")

    def ping() -> DaemonProbe:
        from redis.exceptions import RedisError

        try:
            _default_redis_ping(redis_url, connection=connection)
        except RedisError as exc:
            return DaemonProbe.down(f"native Redis runtime ACL PING failed: {type(exc).__name__}")
        return DaemonProbe.up("Redis runtime ACL authenticated and answered PING")

    return ping()


def _default_get_record(home: Path) -> ClusterRecord | None:
    from base.cluster import get_record

    return get_record(home)


def _default_pooler_admin_password(home: Path) -> str:
    from base.host.db_authority.delivery import read_pooler_admin

    return read_pooler_admin(home.resolve()).password


def _default_loopback_reachable(port: int, password: str) -> bool:
    from base.cluster.dataplane import pooler

    return pooler.pgbouncer_listener_reachable(port, password)


def _default_public_reachable(port: int, user: str, *, machine: MachineIdentityReader) -> bool:
    from base.cluster.dataplane import pooler

    return pooler.pgbouncer_public_listener_reachable(port, user, machine=machine)


def pgbouncer(
    *,
    machine: MachineIdentityReader,
    home: Path,
    record: ClusterRecord | None = None,
    admin_password: str | None = None,
    loopback: bool | None = None,
    public: bool | None = None,
) -> DaemonProbe:
    from base.host.db_authority.delivery import POOLER_ADMIN
    from base.host.db_authority.model import AuthorityRefusedError

    resolved_record = _default_get_record(home) if record is None else record
    if resolved_record is None:
        return DaemonProbe.unavailable("no registry record for the local pooler")
    port = resolved_record.ports["pgbouncer"]
    if admin_password is None:
        try:
            resolved_password = _default_pooler_admin_password(home)
        except AuthorityRefusedError as exc:
            return DaemonProbe.unavailable(f"no pooler admin credential: {exc}")
    else:
        resolved_password = admin_password

    def protocol() -> DaemonProbe:
        loopback_ok = (
            _default_loopback_reachable(port, resolved_password) if loopback is None else loopback
        )
        public_ok = loopback_ok and (
            _default_public_reachable(port, POOLER_ADMIN, machine=machine)
            if public is None
            else public
        )
        if loopback_ok and public_ok:
            return DaemonProbe.up("native pooler admin console and required listeners answered")
        return DaemonProbe.down(
            f"pooler listener failure: loopback={loopback_ok}, public={public_ok}"
        )

    return protocol()


def browser_reach(
    *,
    url: str,
    port: int,
    profile: Path,
    timeout_s: float,
    browser_verdict: DaemonProbe | None = None,
    canary_result: CanaryResult | None = None,
    host_result: HostResult | None = None,
) -> DaemonProbe:
    resolved_url = url.strip()
    if not resolved_url:
        return DaemonProbe.unavailable("gateway reachability URL is not configured")

    def protocol() -> DaemonProbe:
        browser = (
            _default_probe_browser(port, profile) if browser_verdict is None else browser_verdict
        )
        if not browser.alive:
            return DaemonProbe.unavailable(f"browser canary prerequisite failed: {browser.detail}")
        canary = (
            _default_canary(port, resolved_url, timeout_s)
            if canary_result is None
            else canary_result
        )
        if canary.outcome == "skip":
            return DaemonProbe.unavailable(canary.detail)
        if canary.outcome == "ok":
            return DaemonProbe.up(canary.detail)
        host = _default_host_probe(resolved_url, timeout_s) if host_result is None else host_result
        detail = f"browser={canary.detail}; host={host.detail}"
        if not host.ok:
            return DaemonProbe.unavailable(f"host baseline also failed: {detail}")
        return DaemonProbe.down(f"browser-specific reachability failure: {detail}")

    return protocol()


# meiva:note
# One bounded `SELECT 1` — the dependency signal that closes an open breaker. The
# 2026-10-18 outage made every machine's agent host exit on a database renewal
# failure, and once the database answered again nothing brought them back (task
# #5315); while a breaker is open, one cheap round trip per health round says
# whether the dependency recovered. A wedged cluster fails the observation, never
# the process: the probe runner's deadline turns an overdue worker into UNAVAILABLE,
# and an unobserved database never closes a breaker.
# meiva:link services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md
def database_recovery_probe(database: Callable[[], Database]) -> Callable[[], DaemonProbe]:

    def probe() -> DaemonProbe:
        import psycopg

        try:
            with database().connect(autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.OperationalError as exc:
            return DaemonProbe.down(f"database unreachable: {type(exc).__name__}")
        except Exception as exc:
            return DaemonProbe.unavailable(f"database probe failed: {type(exc).__name__}")
        return DaemonProbe.up("database answered")

    return probe


# meiva:note
# The reminder record the stall diagnostics read: the digests the task-maintenance daemon
# delivered, and the telemetry rows each delivery emits. The delivered count is the
# daemon's own durable record, so a fleet with nothing due reports a quiet healthy sample
# instead of an alarm. Delivery without stored telemetry is the failure this exists for —
# from 2026-10-03 the daemon kept delivering >100 digests a day while a storage filter
# (events without `persist`) dropped every row, and nothing said so.
#
# One day is the window because the fleet's own delivery rate makes it unambiguous: the
# daemon delivered 45-130 check-in digests in every 24 hours measured, so a day of
# deliveries with no stored row is a broken record, never a quiet stretch.
TASK_TELEMETRY_WINDOW_HOURS = 24
_TASK_TELEMETRY_SQL = """
SELECT
    (SELECT count(*) FROM inbound_messages
      WHERE kind = 'system_note' AND payload->>'note_tag' = 'task'
        AND created_at > now() - make_interval(hours => %s)),
    (SELECT count(*) FROM telemetry_events
      WHERE event_name IN ('task_reminder_digest', 'task_reminder_rerouted', 'task_escalation')
        AND ts > now() - make_interval(hours => %s))
"""


# meiva:note
# Both sides in one round trip: a sample is one instant of the record, so a delivery cannot
# be counted on one side and missed on the other by a read that straddled it. The delivered
# side is read from `inbound_messages` because that row is written in the same transaction
# as the counter advance, while the telemetry the delivery emits is emitted after commit.
def task_telemetry(database: Callable[[], Database]) -> Callable[[], DaemonProbe]:
    def probe() -> DaemonProbe:
        import psycopg

        try:
            with database().connect(autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(
                    _TASK_TELEMETRY_SQL,
                    (TASK_TELEMETRY_WINDOW_HOURS, TASK_TELEMETRY_WINDOW_HOURS),
                )
                row = cur.fetchone()
        except psycopg.Error as exc:
            return DaemonProbe.unavailable(f"reminder record unread: {type(exc).__name__}")
        delivered, stored = (0, 0) if row is None else (int(row[0]), int(row[1]))
        if delivered and not stored:
            return DaemonProbe.down(
                f"{delivered} reminder notice(s) delivered in {TASK_TELEMETRY_WINDOW_HOURS}h "
                "but telemetry_events holds no task reminder or escalation row"
            )
        return DaemonProbe.up(
            f"{delivered} reminder notice(s) delivered and {stored} task telemetry row(s) "
            f"stored in {TASK_TELEMETRY_WINDOW_HOURS}h"
        )

    return probe


# meiva:note
# The watchdog asks "would restarting this unit strand a live takeover?" and gets
# `(lease_id, holder)` or None. The answer is read here, on the glue side, so the monitor
# keeps no database client of its own: a database outage must not make the watchdog blind.
def live_lease_probe(database: Callable[[], Database], machine: str) -> LeaseProbe:
    def probe() -> tuple[str, str] | None:
        with database().connect(autocommit=True) as conn:
            return live_lease_on_machine(conn, machine)

    return probe


class StationProbe:
    """Gateway-only remote protocol observation."""

    def __init__(
        self,
        *,
        database: Callable[[], Database],
        config: StationEndpointConfig,
        home: Path,
        open_url: UrlOpener,
    ) -> None:
        from services.wake.heartbeat import station_probe

        self._module = station_probe
        self._database = database
        self._config = config
        self._home = home
        self._open_url = open_url

    def probe(self) -> DaemonProbe:
        token = read_telemetry_token(self._home)
        if token is None:
            return DaemonProbe.unavailable("station probe lacks its authentication credential")
        target = self._module.resolve_target(database=self._database, config=self._config)
        if target is None:
            return DaemonProbe.unavailable("configured station target could not be resolved")
        answer = self._module.station_answers(target.url, token=token, open_url=self._open_url)
        if answer is None:
            return DaemonProbe.unavailable("station probe lacks its authentication credential")
        if answer:
            return DaemonProbe.up("remote station authenticated OTLP request accepted")
        return DaemonProbe.down("remote station OTLP ingress failed")


def lgtm_write_path(*, loki_url: str, clock: Clock) -> DaemonProbe:
    from services.supervision.healthchecks import lgtm

    port = urlsplit(loki_url).port
    if port is None:
        return DaemonProbe.unavailable("native Loki endpoint has no explicit port")

    def protocol() -> DaemonProbe:
        ok, reason = lgtm.write_path_probe(loki_url, clock=clock)
        return DaemonProbe.up(reason) if ok else DaemonProbe.down(reason)

    return protocol()


class LokiReport:
    """Retain write-path failure events without granting a backend restart verb."""

    def __init__(self) -> None:
        self._failures = 0
        self._throttles = 0

    def __call__(self, result: DaemonProbe) -> None:
        if result.alive:
            self._failures = 0
            self._throttles = 0
            return
        from base.log import logger

        if result.detail == "push_http_429":
            self._failures = 0
            self._throttles += 1
            if self._throttles == 3 or self._throttles % 30 == 0:
                logger.warning(
                    "Loki write path remains throttled",
                    event="loki_write_path_probe_throttled",
                    consecutive_throttles=self._throttles,
                    reason=result.detail,
                )
            return
        self._throttles = 0
        self._failures += 1

        logger.warning(
            "Loki write path failed: {reason}",
            event="loki_write_path_probe_failed",
            consecutive_failures=self._failures,
            reason=result.detail,
        )


def build_diagnostics(
    requested: set[str],
    *,
    database: Callable[[], Database],
    machine: MachineIdentityReader,
    config: ProbeConfig,
    clock: Clock,
    macos: bool,
    launchd: LaunchdFacts,
    run: CommandRunner,
    root: OwnedProcess,
) -> list[Diagnostic]:
    """Host policy is explicit; absent capabilities do not create fake samples."""
    checks: list[Diagnostic] = []
    # meiva:note
    # A host check spends one deadline (`PROBE_DEADLINE_S`) and the observer allows it a longer one
    # (`DIAGNOSTIC_TIMEOUT_S`), so the probe always returns its own verdict — including "no verdict"
    # — before the sample is killed and reported as a deadline miss.
    if macos:
        checks.append(
            Diagnostic(
                "brew-pin",
                partial(brew_pins, environment=config.environment, deadline_s=PROBE_DEADLINE_S),
                timeout_s=DIAGNOSTIC_TIMEOUT_S,
            )
        )
    checks.append(
        Diagnostic(
            "venv",
            partial(
                venv,
                environment=config.environment,
                run=run,
                clock=clock,
                deadline_s=PROBE_DEADLINE_S,
            ),
            timeout_s=DIAGNOSTIC_TIMEOUT_S,
        )
    )
    if macos:
        from services.supervision.healthchecks.permissions_helper import episode_reporter, probe

        checks.append(
            Diagnostic(
                "permissions-helper",
                partial(
                    probe,
                    home=config.home,
                    facts=launchd,
                    run=run,
                    sock_path=config.helper_socket,
                    root=root,
                ),
                report=episode_reporter(),
            )
        )
    if "gateway" in machine.role():
        is_remote = names_foreign_host(config.db_url, config.redis_url)
        if not is_remote:
            checks.append(Diagnostic("redis-acl", partial(redis_acl, redis_url=config.redis_url)))
            if config.pgbouncer_enabled:
                checks.append(
                    Diagnostic("pgbouncer", partial(pgbouncer, machine=machine, home=config.home))
                )
        # meiva:note
        # Cluster-wide record, one observer: the delivery comparison reads the whole fleet's
        # rows, so any other host's root would report the same finding.
        checks.append(Diagnostic("task-telemetry", task_telemetry(database), interval_s=3600))
        obs_url = config.observability_url.strip()
        if obs_url:
            station = StationProbe(
                database=database,
                config=StationEndpointConfig(
                    observability_url=obs_url,
                    otlp_port=config.observability_otlp_port,
                ),
                home=config.home,
                open_url=SYSTEM_URL_OPENER,
            )
            checks.append(Diagnostic("observatory-station", station.probe))
    if "browser" in requested:
        timeout_s = config.browser_reach_timeout_s
        checks.append(
            Diagnostic(
                "browser-reach",
                partial(
                    browser_reach,
                    url=config.gateway_health_url,
                    port=config.browser_cdp_port,
                    profile=config.browser_profile,
                    timeout_s=timeout_s,
                ),
                interval_s=max(60.0, config.browser_reach_probe_interval_s),
                timeout_s=max(20.0, timeout_s * 3 + 10),
            )
        )
    if "loki" in requested:
        checks.append(
            Diagnostic(
                "lgtm-write-path",
                partial(lgtm_write_path, loki_url=config.loki_url, clock=clock),
                report=LokiReport(),
            )
        )
    return checks
