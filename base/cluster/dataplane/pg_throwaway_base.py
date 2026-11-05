from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from base.cluster.dataplane.pg_plan import (
    InsufficientThrowawaySpaceError,
    ThrowawayConfig,
    choose_throwaway_base,
    dedupe_throwaway_roots,
)
from base.host.env.agent_env import temp_dir_of
from base.log import logger

__all__ = [
    "InsufficientThrowawaySpaceError",
    "ThrowawayConfig",
    "configured_base",
    "default_base",
    "disk_fallback_base",
    "format_bytes",
    "free_bytes",
    "select_throwaway_base",
    "throwaway_roots",
]


def configured_base(raw: str) -> Path | None:
    raw = raw.strip()
    return Path(raw) if raw else None


def format_bytes(n: int) -> str:
    if n >= 2**30:
        return f"{n / 2**30:.1f} GiB"
    return f"{n / 2**20:.0f} MiB"


# meiva:note
# `/dev/shm` is the deliberate tmpfs target on Linux for throwaway cluster data
# directories; fall back to the temp root `environ` names when `/dev/shm` is absent. The temp root
# comes from the environment the caller handed in rather than from `tempfile`, which reads the one
# this process happens to run under: the postmaster is started with the caller's dict, so the two
# answers must be the same one.
def default_base(environ: Mapping[str, str]) -> Path:
    shm = Path("/dev/shm")  # noqa: S108
    if shm.is_dir():
        return shm
    return temp_dir_of(environ)


# meiva:note
# `/var/tmp` is the deliberate disk-backed fallback when `/dev/shm` lacks the
# required free space for scratch clusters; the temp root `environ` names is the last resort, for the
# same reason as `default_base` above.
def disk_fallback_base(environ: Mapping[str, str]) -> Path:
    var_tmp = Path("/var/tmp")  # noqa: S108
    if var_tmp.is_dir():
        return var_tmp
    return temp_dir_of(environ)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def throwaway_roots(
    override: Path | None,
    *,
    environ: Mapping[str, str],
    default_root: Path | None = None,
) -> tuple[Path, ...]:
    default = default_root if default_root is not None else default_base(environ)
    fallback = default_root if default_root is not None else disk_fallback_base(environ)
    return dedupe_throwaway_roots(override, default, fallback)


def select_throwaway_base(
    required_bytes: int | None = None,
    *,
    override: Path | None,
    environ: Mapping[str, str],
    default_root: Path | None = None,
) -> Path:
    default = default_root if default_root is not None else default_base(environ)
    fallback = default_root if default_root is not None else disk_fallback_base(environ)
    free_space: dict[Path, int] = {}
    if override is not None:
        is_dir = override.is_dir()
        if is_dir and required_bytes is not None:
            free_space[override] = free_bytes(override)
        chosen, warning = choose_throwaway_base(
            required_bytes,
            override=override,
            override_is_dir=is_dir,
            default=default,
            fallback=fallback,
            free_space=free_space,
        )
        if warning is not None:
            logger.warning(warning)
        return chosen

    if required_bytes is not None:
        for path in dict.fromkeys((default, fallback)):
            free_space[path] = free_bytes(path)
    chosen, _ = choose_throwaway_base(
        required_bytes,
        override=None,
        default=default,
        fallback=fallback,
        free_space=free_space,
    )
    return chosen
