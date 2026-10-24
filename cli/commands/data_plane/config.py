# meiva:note
# What the data-plane commands read from configuration, as one immutable value.
#
# The composition roots are the process entries: the `meiva start`, `meiva stop` and `meiva status`
# steps in `cli/commands/lifecycle` and the pooler converge step. They read the invocation's
# configuration once (`meiva start` through `start/start_config.py`, with its other values), and
# everything below them receives the result as `config`, so a test hands a function the exact values
# it wants with `dataclasses.replace` instead of patching `settings`.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from base.config import ConfigBoot, settings
from base.host.net.url_secret import names_foreign_host

__all__ = ["DataPlaneConfig", "data_plane_config_from_boot", "data_plane_config_from_settings"]


# meiva:note
# The `data_plane` and `services` fields these commands read.
@dataclass(frozen=True)
class DataPlaneConfig:
    # meiva:note
    # The URLs carry the login and the two passwords are secrets: none of them reaches a
    # repr, so a log line or a traceback that formats the config never prints them.
    db_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    redis_admin_password: str = field(repr=False)
    cluster_secret: str = field(repr=False)
    pgbouncer_enabled: bool
    pooler_max_client_conn: int
    redis_bin_dir: str
    trusted_cidrs: str
    events_channel: str
    memory_search_backend: str
    embedding_backend: str
    walg_config_file: Path | None

    # meiva:note
    # Whether the data plane is hosted off-box: derived from the URLs, never stored, so a copy with
    # a swapped URL answers for that URL (same rule as the settings' own `is_remote`).
    @property
    def is_remote(self) -> bool:
        return names_foreign_host(self.db_url, self.redis_url)


# meiva:note
# The config as the process settings hold it now: the composition roots call this once.
def data_plane_config_from_settings() -> DataPlaneConfig:
    return _from_view(settings)


# meiva:note
# The same config read from one explicit configuration owner (tests build theirs this way).
def data_plane_config_from_boot(boot: ConfigBoot) -> DataPlaneConfig:
    return _from_view(cast("_ConfigView", boot.view))


# meiva:note
# The three domains of a configuration owner this config reads. Importing the `Settings` model to
# name them would make every importer of this module build the eager configuration, which the
# settings-free commands must not do, so the shape is spelled out here.
class _DataPlaneDomain(Protocol):
    @property
    def db_url(self) -> str: ...
    @property
    def redis_url(self) -> str: ...
    @property
    def redis_admin_password(self) -> str: ...
    @property
    def cluster_secret(self) -> str: ...
    @property
    def pgbouncer_enabled(self) -> bool: ...
    @property
    def pooler_max_client_conn(self) -> int: ...
    @property
    def redis_bin_dir(self) -> str: ...
    @property
    def trusted_cidrs(self) -> str: ...
    @property
    def events_channel(self) -> str: ...


class _ServicesDomain(Protocol):
    @property
    def memory_search_backend(self) -> str: ...
    @property
    def embedding_backend(self) -> str: ...


class _WalgDomain(Protocol):
    @property
    def walg_config_file(self) -> Path | None: ...


class _ConfigView(Protocol):
    @property
    def data_plane(self) -> _DataPlaneDomain: ...
    @property
    def services(self) -> _ServicesDomain: ...
    @property
    def walg(self) -> _WalgDomain: ...


# meiva:note
# A configuration owner's view resolves the owner's own model on each read, so the process
# settings and an explicit owner map through the same function.
def _from_view(view: _ConfigView) -> DataPlaneConfig:
    plane = view.data_plane
    return DataPlaneConfig(
        db_url=plane.db_url,
        redis_url=plane.redis_url,
        redis_admin_password=plane.redis_admin_password,
        cluster_secret=plane.cluster_secret,
        pgbouncer_enabled=plane.pgbouncer_enabled,
        pooler_max_client_conn=plane.pooler_max_client_conn,
        redis_bin_dir=plane.redis_bin_dir,
        trusted_cidrs=plane.trusted_cidrs,
        events_channel=plane.events_channel,
        memory_search_backend=view.services.memory_search_backend,
        embedding_backend=view.services.embedding_backend,
        walg_config_file=view.walg.walg_config_file,
    )
