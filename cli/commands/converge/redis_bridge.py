# meiva:note
# Retire the legacy launchd Redis bridge and observe the root-supervised relay.
#
# Redis remains bound to loopback. On a split gateway the `redis-bridge` roster unit
# (`services/redis_bridge/relay.py`, supervised by the root like every other service) accepts the
# cluster's authenticated private-network traffic and forwards it to the same port on
# `127.0.0.1`. Converge only removes the standalone launchd job and installed copy an earlier
# layout used, so exactly one supervisor owns the listener.

from __future__ import annotations

import logging
from pathlib import Path
from typing import assert_never

from base.clock import Timers
from base.cluster.dataplane.redis_bridge import (
    RedisBridgeUnconfiguredError,
    redis_bridge_endpoint,
)
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.host.net.url_secret import url_with_host
from base.host.system.hostport import Host
from cli.commands.converge.plans.bridge_retirement import (
    Expired,
    Leave,
    Retry,
    bootout_wait_step,
    plan_retirement,
    still_loaded_message,
)
from cli.commands.converge.plans.bridge_status import (
    RedisBridgeStatus,
    failure_detail,
    not_required,
    ping_status,
    status_line,
    unconfigured,
)
from cli.commands.converge.spec import ConvergeCtx

logger = logging.getLogger("cli.converge.redis_bridge")

LABEL = "com.meiva.redis-bridge"
_BOOTOUT_TIMEOUT_S = 10.0
_BOOTOUT_POLL_INTERVAL_S = 0.25
_LAUNCHCTL_TIMEOUT_S = 30.0
_PING_TIMEOUT_S = 3.0


def _installed_path(home: Path) -> Path:
    return home / "redis-bridge" / "relay.py"


# meiva:note
# Where the standalone LaunchAgent's plist lived, under the invoking user's home.
def launchd_plist_path(user_home: Path) -> Path:
    return user_home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def job_loaded(host: Host) -> bool:
    result = host.run(
        ["launchctl", "print", f"gui/{host.uid}/{LABEL}"],
        capture_output=True,
        text=True,
        timeout=_LAUNCHCTL_TIMEOUT_S,
    )
    return result.returncode == 0


# meiva:note
# Unload the job and wait, up to `timeout_s`, for launchd to stop reporting it; False when it
# stayed loaded.
def bootout_and_wait(host: Host, *, timers: Timers, timeout_s: float = _BOOTOUT_TIMEOUT_S) -> bool:
    host.run(
        ["launchctl", "bootout", f"gui/{host.uid}/{LABEL}"],
        capture_output=True,
        text=True,
        timeout=_LAUNCHCTL_TIMEOUT_S,
    )
    started = timers.monotonic()
    while job_loaded(host):
        wait = bootout_wait_step(
            elapsed_s=timers.monotonic() - started,
            timeout_s=timeout_s,
            interval_s=_BOOTOUT_POLL_INTERVAL_S,
        )
        match wait:
            case Expired():
                return False
            case Retry(after_s):
                timers.sleep(after_s)
            case _:
                assert_never(wait)
    return True


# meiva:note
# Unload and remove the standalone launchd job and installed copy.
def retire_launchd_bridge(
    home: Path,
    *,
    host: Host,
    user_home: Path,
    timers: Timers,
    timeout_s: float = _BOOTOUT_TIMEOUT_S,
) -> None:
    plist_path = launchd_plist_path(user_home)
    installed_path = _installed_path(home)
    plan = plan_retirement(
        loaded=job_loaded(host),
        plist_present=plist_path.exists(),
        installed_present=installed_path.exists(),
    )
    if isinstance(plan, Leave):
        return
    if plan.unload and not bootout_and_wait(host, timers=timers, timeout_s=timeout_s):
        raise RuntimeError(
            still_loaded_message(label=LABEL, timeout_s=timeout_s, plist_path=str(plist_path))
        )
    plist_path.unlink(missing_ok=True)
    installed_path.unlink(missing_ok=True)
    logger.info("Retired the standalone Redis bridge LaunchAgent and installed source")


# meiva:note
# Leave the relay to the root supervisor: no standalone launchd job may hold its port.
def retire_launchd_redis_bridge(ctx: ConvergeCtx) -> None:
    if ctx.host.platform.system == "darwin":
        retire_launchd_bridge(
            ctx.meiva_home, host=ctx.host, user_home=ctx.session.user_home, timers=ctx.timers
        )


# meiva:note
# PING Redis at `url` with the URL's own credentials; None when it answered, else why not.
#
# redis-py's stubs leave `from_url` and `ping` untyped, which is what the two pyright ignores
# below accept.
def ping_redis(url: str) -> str | None:
    import redis

    try:
        client = redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            url, socket_connect_timeout=_PING_TIMEOUT_S, socket_timeout=_PING_TIMEOUT_S
        )
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()
    except Exception as exc:
        return failure_detail(str(exc), type(exc).__name__)
    return None


# meiva:note
# PING Redis through the relay's private-network endpoint, on a host of `platform`.
#
# The relay exists only on macOS with a declared non-loopback address and a local data plane;
# anywhere else the status says it is not required, without reading the machine's identity.
def probe_bridge_on(
    platform: str, home: Path, *, machine: MachineIdentityReader, config: ConfigBoot
) -> RedisBridgeStatus:
    try:
        bridge = redis_bridge_endpoint(
            home,
            machine_host=machine.host,
            remote_data_plane=lambda: config.view.data_plane.is_remote,
            platform=platform,
        )
    except RedisBridgeUnconfiguredError as exc:
        return unconfigured(str(exc))
    if bridge is None:
        return not_required()
    endpoint = f"{bridge.listen_host}:{bridge.port}"
    try:
        url = url_with_host(config.view.data_plane.redis_url, bridge.listen_host)
    except Exception as exc:
        return ping_status(endpoint, failure_detail(str(exc), type(exc).__name__))
    return ping_status(endpoint, ping_redis(url))


# meiva:note
# The status of the relay of `home`, on the platform of `host`.
def probe_redis_bridge(
    home: Path, *, host: Host, machine: MachineIdentityReader, config: ConfigBoot
) -> RedisBridgeStatus:
    return probe_bridge_on(host.platform.system, home, machine=machine, config=config)


def print_redis_bridge_status(
    home: Path, *, host: Host, machine: MachineIdentityReader, config: ConfigBoot
) -> None:
    print(status_line(probe_redis_bridge(home, host=host, machine=machine, config=config)))
