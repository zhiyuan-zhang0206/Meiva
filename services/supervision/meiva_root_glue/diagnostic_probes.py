"""Deployment diagnostics: protocol observation and alerting.

Endpoint configuration and authentication follow their existing owners. No adapter calls
an ensure, start, stop, launchd repair, or session command.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from base.agents.impersonation import live_lease_on_machine
from base.cluster.machine import MachineIdentityReader
from base.cluster.record import ClusterRecord
from base.config import get_field
from base.daemon.health import DaemonProbe
from base.db import Database
from base.host import brew_pin, proc
from base.host.db_authority.api import read_telemetry_token
from base.host.net.ports import SYSTEM_URL_OPENER, UrlOpener
from base.host.net.url_secret import names_foreign_host
from base.native_process.os_platform import is_macos
from base.telemetry.station_endpoint import StationEndpointConfig
from services.desktop.browser.probe import probe_browser as _default_probe_browser
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
from services.supervision.healthchecks.redis_acl import ping as _default_redis_ping
from services.supervision.meiva_root.health import LeaseProbe
from services.supervision.meiva_root_glue.diagnostics import Diagnostic

FieldReader = Callable[[str], str | int | float | bool]


def brew_pins(
    *,
    brew_binary: str = "brew",
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = proc.run_bounded,
) -> DaemonProbe:
    observed: list[set[str]] = []
    for option in ("--pinned", "--formula"):
        result = run_cmd([brew_binary, "list", option], timeout=5, capture_output=True, text=True)
        result.check_returncode()
        observed.append(set(result.stdout.splitlines()))
    pinned, installed = observed
    missing = sorted((brew_pin.PINNED_BREW_FORMULAE & installed) - pinned)
    if missing:
        return DaemonProbe.down(f"approved Homebrew formulae are unpinned: {', '.join(missing)}")
    return DaemonProbe.up("installed approved Homebrew formulae are pinned")


def venv() -> DaemonProbe:
    from base.native_process.child_env import inherited_process_env
    from services.supervision.healthchecks import prod_venv

    uv = shutil.which("uv")
    if uv is None:
        return DaemonProbe.unavailable("uv unavailable: dependency consistency was not measured")
    # meiva:note
    # Inspect the checkout actually executing root, never a different cluster's
    # production checkout.
    violations = prod_venv.venv_violations(
        source_root=Path(__file__).resolve().parents[3],
        uv=uv,
        environment=inherited_process_env(),
    )
    if violations:
        return DaemonProbe.down("; ".join(violations))
    return DaemonProbe.up("root checkout dependency and import checks passed")


def redis_acl(
    *,
    read_field: FieldReader = get_field,
    ping_fn: Callable[[str], None] = _default_redis_ping,
) -> DaemonProbe:
    from base.cluster import ownership

    redis_url = str(read_field("redis_url"))
    port = ownership.redis_port_of(redis_url)
    if port is None:
        return DaemonProbe.unavailable("no explicit local Redis endpoint")

    def ping() -> DaemonProbe:
        from redis.exceptions import RedisError

        try:
            ping_fn(redis_url)
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
    record_reader: Callable[[Path], ClusterRecord | None] = _default_get_record,
    admin_password_reader: Callable[[Path], str] = _default_pooler_admin_password,
    loopback_reachable: Callable[[int, str], bool] = _default_loopback_reachable,
    public_reachable: Callable[..., bool] = _default_public_reachable,
) -> DaemonProbe:
    from base.host.db_authority.delivery import POOLER_ADMIN
    from base.host.db_authority.model import AuthorityRefusedError
    from base.paths import meiva_home

    home = meiva_home()
    record = record_reader(home)
    if record is None:
        return DaemonProbe.unavailable("no registry record for the local pooler")
    port = record.ports["pgbouncer"]
    try:
        admin_password = admin_password_reader(home)
    except AuthorityRefusedError as exc:
        return DaemonProbe.unavailable(f"no pooler admin credential: {exc}")

    def protocol() -> DaemonProbe:
        loopback = loopback_reachable(port, admin_password)
        public = loopback and public_reachable(port, POOLER_ADMIN, machine=machine)
        if loopback and public:
            return DaemonProbe.up("native pooler admin console and required listeners answered")
        return DaemonProbe.down(f"pooler listener failure: loopback={loopback}, public={public}")

    return protocol()


def browser_reach(
    *,
    read_field: FieldReader = get_field,
    browser_probe: Callable[[], DaemonProbe] = _default_probe_browser,
    canary_fn: Callable[[int, str, float], CanaryResult] = _default_canary,
    host_probe_fn: Callable[[str, float], HostResult] = _default_host_probe,
) -> DaemonProbe:
    url = str(read_field("gateway_health_url")).strip()
    if not url:
        return DaemonProbe.unavailable("gateway reachability URL is not configured")
    port = int(read_field("browser_cdp_port"))
    timeout_s = float(read_field("browser_reach_timeout_s"))

    def protocol() -> DaemonProbe:
        browser = browser_probe()
        if not browser.alive:
            return DaemonProbe.unavailable(f"browser canary prerequisite failed: {browser.detail}")
        canary = canary_fn(port, url, timeout_s)
        if canary.outcome == "skip":
            return DaemonProbe.unavailable(canary.detail)
        if canary.outcome == "ok":
            return DaemonProbe.up(canary.detail)
        host = host_probe_fn(url, timeout_s)
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


def lgtm_write_path() -> DaemonProbe:
    from base.telemetry.lgtm_local import backend_urls
    from services.supervision.healthchecks import lgtm

    loki_url = backend_urls()["loki"]
    port = urlsplit(loki_url).port
    if port is None:
        return DaemonProbe.unavailable("native Loki endpoint has no explicit port")

    def protocol() -> DaemonProbe:
        ok, reason = lgtm.write_path_probe(loki_url)
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
    read_field: FieldReader = get_field,
    macos_fn: Callable[[], bool] = is_macos,
) -> list[Diagnostic]:
    """Host policy is explicit; absent capabilities do not create fake samples."""
    checks: list[Diagnostic] = []
    if macos_fn():
        checks.append(Diagnostic("brew-pin", brew_pins))
    checks.append(Diagnostic("venv", venv))
    if macos_fn():
        from services.supervision.healthchecks.permissions_helper import episode_reporter, probe

        checks.append(Diagnostic("permissions-helper", probe, report=episode_reporter()))
    if "gateway" in machine.role():
        is_remote = names_foreign_host(str(read_field("db_url")), str(read_field("redis_url")))
        if not is_remote:
            checks.append(Diagnostic("redis-acl", partial(redis_acl, read_field=read_field)))
            if bool(read_field("pgbouncer_enabled")):
                checks.append(Diagnostic("pgbouncer", partial(pgbouncer, machine=machine)))
        obs_url = str(read_field("observability_url")).strip()
        if obs_url:
            from base.paths import meiva_home

            station = StationProbe(
                database=database,
                config=StationEndpointConfig(
                    observability_url=obs_url,
                    otlp_port=int(read_field("observability_otlp_port")),
                ),
                home=meiva_home().resolve(),
                open_url=SYSTEM_URL_OPENER,
            )
            checks.append(Diagnostic("observatory-station", station.probe))
    if "browser" in requested:
        checks.append(
            Diagnostic(
                "browser-reach",
                partial(browser_reach, read_field=read_field),
                interval_s=max(60.0, float(read_field("browser_reach_probe_interval_s"))),
                timeout_s=max(20.0, float(read_field("browser_reach_timeout_s")) * 3 + 10),
            )
        )
    if "loki" in requested:
        checks.append(Diagnostic("lgtm-write-path", lgtm_write_path, report=LokiReport()))
    return checks
