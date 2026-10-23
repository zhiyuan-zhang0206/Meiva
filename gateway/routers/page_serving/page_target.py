# meiva:note
# The registry reads behind the page dial-target guard, and the guard itself.
#
# The decisions (which hosts and ports are allowed) are `page_target_policy`; this module loads
# the agent's home machine and the addresses it advertises and applies them.

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from urllib.parse import urlparse

from fastapi import HTTPException
from psycopg import Connection
from psycopg_pool import ConnectionPool

from gateway.routers.page_serving import page_target_policy
from gateway.routers.refusal import HttpFailure

__all__ = ["PageHostCache", "validate_page_dial_target", "validate_proxy_page_target"]

_PAGE_HOST_CACHE_TTL_S = 60.0
_PAGE_HOST_CACHE_MAX_ENTRIES = 4096


# meiva:note
# Short-lived, LRU-bounded allowlist of dial hosts per machine.
#
# Owned by the gateway app (`app.state.page_host_cache`). Lookups run on
# worker threads (`asyncio.to_thread`), hence the lock.
class PageHostCache:
    def __init__(self, *, max_entries: int = _PAGE_HOST_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._hosts: OrderedDict[str, tuple[float, frozenset[str]]] = OrderedDict()
        self._lock = threading.Lock()

    # meiva:note
    # The machines whose allowlist is cached, least recently used first.
    def cached_machines(self) -> list[str]:
        with self._lock:
            return list(self._hosts)

    # meiva:note
    # Return the cached host allowlist advertised by one machine.
    def dial_hosts(
        self, pool: ConnectionPool, machine: str, *, connection: Connection | None = None
    ) -> frozenset[str]:
        now = time.monotonic()
        with self._lock:
            cached = self._hosts.get(machine)
            if cached is not None and cached[0] > now:
                self._hosts.move_to_end(machine)
                return cached[1]
        hosts = {machine}
        with (
            pool.connection() if connection is None else nullcontext(connection) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT url FROM machine_units "
                "WHERE machine_name = %s AND stopped_at IS NULL AND url IS NOT NULL",
                (machine,),
            )
            for (url,) in cur.fetchall():
                hostname = urlparse(str(url)).hostname
                if hostname:
                    hosts.add(hostname)
        allowed = frozenset(hosts)
        with self._lock:
            self._hosts[machine] = (now + _PAGE_HOST_CACHE_TTL_S, allowed)
            self._hosts.move_to_end(machine)
            if len(self._hosts) > self._max_entries:
                self._hosts.popitem(last=False)
        return allowed


# meiva:note
# Return a page agent's registered home machine, if it is usable.
def _agent_machine(
    pool: ConnectionPool, agent_id: int, *, connection: Connection | None = None
) -> str | None:
    with (
        pool.connection() if connection is None else nullcontext(connection) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    machine = row[0] if row is not None else None
    return str(machine) if machine and machine != "unknown" else None


def _refuse(failure: HttpFailure | None) -> None:
    if failure is not None:
        raise HTTPException(status_code=failure.status_code, detail=failure.detail)


def _validate_nonloopback_page_host(
    pool: ConnectionPool,
    host_cache: PageHostCache,
    agent_id: int,
    host: str,
    *,
    status_code: int,
    connection: Connection | None = None,
) -> None:
    machine = _agent_machine(pool, agent_id, connection=connection)
    advertised = (
        frozenset[str]()
        if machine is None
        else host_cache.dial_hosts(pool, machine, connection=connection)
    )
    _refuse(
        page_target_policy.nonloopback_refusal(
            host, agent_id, machine, advertised, status_code=status_code
        )
    )


# meiva:note
# Revalidate non-loopback registry rows immediately before proxy dialing.
#
# Registration validates new rows, but ops writes may predate or bypass that
# path. This short-lived allowlist is read from the same machine registry and
# fails closed before the gateway makes an untrusted network connection.
def validate_proxy_page_target(
    pool: ConnectionPool, host_cache: PageHostCache, agent_id: int, host: str
) -> None:
    if not page_target_policy.is_loopback_host(host):
        _validate_nonloopback_page_host(pool, host_cache, agent_id, host, status_code=403)


# meiva:note
# SSRF guard for the page reverse proxy (audit round-2 P1-4).
#
# The dial target is restricted to loopback or the registering agent's home machine (its
# `agents_meta.machine` name or any host its `machine_units` rows advertise), on a port of
# 1024 or above. Raises HTTPException 400/404; register_page is never reached with a
# foreign target.
def validate_page_dial_target(
    pool: ConnectionPool,
    host_cache: PageHostCache,
    agent_id: int,
    host: str,
    port: int,
    *,
    connection: Connection | None = None,
) -> None:
    _refuse(page_target_policy.privileged_port_refusal(port))
    if page_target_policy.is_loopback_host(host):
        return
    _validate_nonloopback_page_host(
        pool, host_cache, agent_id, host, status_code=400, connection=connection
    )
