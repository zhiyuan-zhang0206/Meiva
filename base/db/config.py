"""The database dial's configuration: one frozen value, read from settings in one place.

`DbConfig` holds exactly what dialing the cluster Postgres decides on. A composition root
builds it (`db_config_from_settings()`, or `Database.from_settings()` in `base.db.handle`) and
hands components the `Database` handle, never the URL: the URL carries the login.
"""

from __future__ import annotations

from typing import Protocol

from base.config import settings
from base.db.protocols import (
    DbConfig as DbConfig,
)
from base.db.protocols import (
    db_config_from_boot as db_config_from_boot,
)


class _DataPlaneView(Protocol):
    @property
    def db_url(self) -> str: ...

    @property
    def db_sslmode(self) -> str: ...

    @property
    def db_pool_min_size(self) -> int: ...

    @property
    def db_pool_max_size(self) -> int: ...

    @property
    def pgbouncer_enabled(self) -> bool: ...


class _EnvBootView(Protocol):
    @property
    def db_authority_refusal(self) -> str | None: ...


def db_config_from_settings() -> DbConfig:
    """Build the slice from the live settings (read at each call, so a test or an overlay that
    changed a field reaches the next dial exactly as the former direct reads did)."""
    return _from_view(settings.data_plane, settings.env_boot)


def _from_view(data_plane: _DataPlaneView, env_boot: _EnvBootView) -> DbConfig:
    return DbConfig(
        db_url=data_plane.db_url,
        db_sslmode=data_plane.db_sslmode,
        db_pool_min_size=data_plane.db_pool_min_size,
        db_pool_max_size=data_plane.db_pool_max_size,
        pgbouncer_enabled=data_plane.pgbouncer_enabled,
        db_authority_refusal=env_boot.db_authority_refusal,
    )


def default_restore_budget_seconds() -> float:
    return settings.data_plane.db_pool_restore_timeout_seconds


def default_restore_client_deadline_seconds() -> float:
    return settings.data_plane.db_pool_restore_client_deadline_seconds
