# meiva:note
# The configuration the scheduler daemon and its worker act on, read once at their roots.
#
# Every field is `restart_required: gateway` in its settings domain, so a running process has
# never seen it change; reading it once at the root says so. Below the root a function takes
# these values, never the config owner, and a test builds them directly.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from base.cluster.dataplane.pg_throwaway_base import configured_base
from base.config import ConfigBoot


# meiva:note
# What the scheduler decides on: the cluster clock and backup hour, the retention it hands its
# commits, and the throwaway base a restore drill is captured against (absolute, because the
# worker starts at the code root and the controller's own sweep must see the same path).
@dataclass(frozen=True)
class SchedulerSettings:
    timezone: ZoneInfo
    backup_hour: int
    keep: int
    throwaway_base: Path | None

    @classmethod
    def read(cls, config: ConfigBoot) -> SchedulerSettings:
        override = configured_base(config.view.data_plane.pg_throwaway_base)
        return cls(
            timezone=ZoneInfo(config.view.general.timezone),
            backup_hour=config.view.services.backup_hour,
            keep=config.view.services.backup_keep,
            throwaway_base=None if override is None else override.absolute(),
        )


# meiva:note
# What one dump worker acts on: which plane it dumps (remote-managed or locally owned) and its
# URL, the retention, and the off-site destination fields.
@dataclass(frozen=True)
class DumpSettings:
    is_remote: bool
    plane_url: str
    keep: int
    offsite_endpoint: str
    offsite_bucket: str
    offsite_credentials_file: Path | None

    @classmethod
    def read(cls, config: ConfigBoot) -> DumpSettings:
        services, plane = config.view.services, config.view.data_plane
        return cls(
            is_remote=plane.is_remote,
            plane_url=plane.db_url,
            keep=services.backup_keep,
            offsite_endpoint=services.backup_offsite_endpoint,
            offsite_bucket=services.backup_offsite_bucket,
            offsite_credentials_file=services.backup_offsite_credentials_file,
        )
