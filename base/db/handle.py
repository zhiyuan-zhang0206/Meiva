"""`Database`: the injected handle to the cluster Postgres.

Components take a `Database` (or a connection/pool it opened), never the URL or `DbConfig`:
the URL carries the login, and only the code that dials needs it. A composition root builds
the handle once (`Database.from_settings(gate=gate)`) and passes it down. Every handle
and cluster dial requires the entry's `ProcessDbGate`; independent pools and live-config
rebuilds retain that same loaded image, admission posture and minimum-read budget.

The dial decisions stay in `base/db/connections.py` (the `postgres-dial` single owner); this
class only binds one `DbConfig` to them.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.db import connections
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig, db_config_from_settings
from base.db.connections import DirectUrlResolver


# meiva:note
# One cluster Postgres, dialed with one `DbConfig`.
class Database:
    def __init__(
        self,
        config: DbConfig,
        *,
        gate: ProcessDbGate,
        direct_url: DirectUrlResolver | None = None,
        dial: Callable[..., psycopg.Connection[Any]] = psycopg.connect,
    ) -> None:
        self._config = config
        self._gate = gate
        self._direct_url = direct_url
        self._dial = dial

    # meiva:note
    # The composition-root constructor: the config as the live settings hold it now.
    @classmethod
    def from_settings(
        cls,
        *,
        gate: ProcessDbGate,
        direct_url: DirectUrlResolver | None = None,
    ) -> Database:
        return cls(db_config_from_settings(), gate=gate, direct_url=direct_url)

    # meiva:note
    # `base.db.connect()` on this handle's config.
    def connect(
        self, *, autocommit: bool = False, direct: bool = False, unbounded: bool = False
    ) -> psycopg.Connection:
        return connections.connect(
            autocommit=autocommit,
            direct=direct,
            unbounded=unbounded,
            config=self._config,
            gate=self._gate,
            direct_url=self._direct_url,
            dial=self._dial,
        )

    def pool(
        self,
        *,
        min_size: int | None = None,
        max_size: int | None = None,
        direct: bool = False,
        timeout: float = connections.DEFAULT_POOL_TIMEOUT_S,
        check_connections: bool = False,
        autocommit: bool = False,
        row_factory: object = None,
    ) -> ConnectionPool:
        """`base.db.pool()` on this handle's config."""
        return connections.pool_with_config(
            self._config,
            min_size=min_size,
            max_size=max_size,
            direct=direct,
            timeout=timeout,
            check_connections=check_connections,
            autocommit=autocommit,
            row_factory=row_factory,
            gate=self._gate,
            direct_url=self._direct_url,
        )

    def async_pool(
        self,
        pool_class: Callable[..., AsyncConnectionPool[psycopg.AsyncConnection]],
        *,
        min_size: int,
        max_size: int,
        timeout: float,
        **pool_kwargs: object,
    ) -> AsyncConnectionPool[psycopg.AsyncConnection]:
        """`base.db.async_pool()` on this handle's config."""
        return connections.async_pool_with_config(
            pool_class,
            self._config,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            gate=self._gate,
            **pool_kwargs,
        )

    def direct_url(self) -> str:
        """The admin-plane URL of this handle's cluster (never through the pooler)."""
        return connections.require_direct(self.direct_resolution())

    def direct_resolution(self) -> connections.DirectResolution:
        return connections.require_direct_url(self._direct_url)(self._config)

    @contextmanager
    def write_transaction(self) -> Generator[psycopg.Connection, None, None]:
        """One explicitly writable transaction on a fresh connection of this handle."""
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ WRITE")
            yield conn
