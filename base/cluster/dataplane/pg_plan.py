from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PG_SHM_ARGS = "-c shared_memory_type=mmap -c dynamic_shared_memory_type=mmap"
PG_SHM_MMAP_OK_ON_MACOS = True
PG_TZ_ARGS = "-c timezone=UTC"
MACOS_PG_LOCALE = "en_US.UTF-8"
POSTMASTER_SHUTDOWN_S = 10.0

DAEMON_ENV_PASS_THROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TZ",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "SYSTEMROOT",
)


def build_pg_shm_args(*, macos: bool, mmap_ok_on_macos: bool = PG_SHM_MMAP_OK_ON_MACOS) -> str:
    if macos and not mmap_ok_on_macos:
        return ""
    return PG_SHM_ARGS


def _filter_daemon_env(raw_env: Mapping[str, str]) -> dict[str, str]:
    return {key: raw_env[key] for key in DAEMON_ENV_PASS_THROUGH if key in raw_env}


def build_pg_start_env(raw_env: Mapping[str, str], *, macos: bool) -> dict[str, str]:
    env = _filter_daemon_env(raw_env)
    if macos and not env.get("LC_ALL") and not env.get("LANG"):
        env["LC_ALL"] = MACOS_PG_LOCALE
    return env


@dataclass(frozen=True)
class ThrowawayConfig:
    override: Path | None
    default_root: Path | None = None
    artifact_dir: Path | None = None
    candidate_ports: tuple[int, ...] = ()
    bin_dir: Path | None = None
    # meiva:note
    # The home whose vendored Postgres runtime serves the throwaway cluster when `bin_dir` names
    # no directory with the tool.
    home: Path = field(kw_only=True)


def _format_bytes(n: int) -> str:
    if n >= 2**30:
        return f"{n / 2**30:.1f} GiB"
    return f"{n / 2**20:.0f} MiB"


class InsufficientThrowawaySpaceError(RuntimeError):
    pass


def dedupe_throwaway_roots(
    override: Path | None,
    default: Path,
    fallback: Path,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for root in (override, default, fallback):
        if root is not None and root not in roots:
            roots.append(root)
    return tuple(roots)


def choose_throwaway_base(
    required_bytes: int | None = None,
    *,
    override: Path | None,
    override_is_dir: bool = True,
    default: Path,
    fallback: Path,
    free_space: Mapping[Path, int] | None = None,
) -> tuple[Path, str | None]:
    space = free_space or {}
    if override is not None:
        if not override_is_dir:
            raise RuntimeError(
                f"MEIVA_PG_THROWAWAY_BASE={override} is not an existing directory — "
                "point it at a directory on a volume with room for scratch clusters"
            )
        warning: str | None = None
        if required_bytes is not None and space.get(override, 0) < required_bytes:
            avail = space.get(override, 0)
            warning = (
                f"throwaway base override {override} offers "
                f"{_format_bytes(avail)}, below the requested "
                f"{_format_bytes(required_bytes)} — proceeding anyway (explicit override)"
            )
        return override, warning

    if required_bytes is None:
        return default, None

    durable = fallback if fallback != default else default
    if space.get(durable, 0) >= required_bytes:
        return durable, None

    offered = ", ".join(
        f"{path} ({_format_bytes(space.get(path, 0))} free)"
        for path in dict.fromkeys((default, fallback))
    )
    raise InsufficientThrowawaySpaceError(
        f"throwaway Postgres needs {_format_bytes(required_bytes)} free on a durable base — "
        f"{offered}. Free space, or point MEIVA_PG_THROWAWAY_BASE at a larger volume."
    )


def verify_foreground_data_dir(reported_dir: str | None, expected_dir: Path) -> None:
    if reported_dir is None or Path(reported_dir).resolve() != expected_dir.resolve():
        raise RuntimeError("foreground Postgres port is owned by another data directory")


def foreground_stop_argv(
    process_args: Sequence[object] | str | bytes | os.PathLike[str] | os.PathLike[bytes],
    *,
    shutdown_s: float = POSTMASTER_SHUTDOWN_S,
) -> list[str]:
    if isinstance(process_args, (str, bytes, os.PathLike)) or not isinstance(process_args, list):
        raise TypeError("foreground Postgres requires an argv list")
    command: list[str] = []
    for arg in process_args:
        if not isinstance(arg, str):
            raise TypeError("foreground Postgres requires an argv list")
        command.append(arg)
    try:
        data = command[command.index("-D") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(
            "foreground Postgres must name its private data directory with -D"
        ) from exc
    pg_ctl = Path(command[0]).with_name("pg_ctl")
    return [
        str(pg_ctl),
        "-D",
        data,
        "stop",
        "-m",
        "immediate",
        "-w",
        "-t",
        str(int(shutdown_s)),
    ]
