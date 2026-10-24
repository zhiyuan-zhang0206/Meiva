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

from base.config import ConfigBoot
from base.host.net.url_secret import names_foreign_host

__all__ = ["DataPlaneConfig", "data_plane_config_from_boot"]


# meiva:note
# The `data_plane`, `services` and `walg` fields these commands read.
#
# `walg_config_file` is carried here and nowhere else: a command that needs the WAL-G
# configuration path takes it from this value.
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
# The config read from one configuration owner's view: a command's entry passes `command.config`,
# a test passes the owner it built. Reading a field resolves it on that owner, so the values are
# the ones that owner holds now; the settings models are never named here, which keeps importing
# this module from building the eager configuration.
def data_plane_config_from_boot(boot: ConfigBoot) -> DataPlaneConfig:
    view = boot.view
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
