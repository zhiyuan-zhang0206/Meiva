"""Provision the same PostgreSQL installation that runtime resolution selects."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from base.cluster.dataplane import runtime_binaries
from base.cluster.dataplane.pg_tools import pg_tool
from base.host.net.ports import UrlOpener
from base.host.system.backend import get_backend
from base.native_process.os_platform import is_macos


def installed_server(
    *,
    platform_bin_dir: Path | None = None,
    path_env: str | None = None,
) -> Path | None:
    if platform_bin_dir is not None:
        candidate: Path | None = platform_bin_dir / "postgres"
    else:
        candidate = get_backend().pg_binary_path("postgres")
    if candidate is not None and candidate.exists():
        return candidate
    found = (
        shutil.which("postgres") if path_env is None else shutil.which("postgres", path=path_env)
    )
    return Path(found) if found is not None else None


def _output(binary: Path, option: str) -> str:
    result = subprocess.run(  # noqa: S603 — resolved local PG executable, fixed inspection option
        [str(binary), option], capture_output=True, text=True, check=True, timeout=10
    )
    return result.stdout.strip()


def _require_pg17(binary: Path) -> None:
    version = _output(binary, "--version")
    if re.search(r"\bPostgreSQL\)?\s+17\.\d+(?=\s|$)", version) is None:
        raise RuntimeError(f"PostgreSQL 17 is required: {binary} reports {version!r}")


def _verify_installed_runtime(
    server: Path,
    *,
    home: Path,
    platform_bin_dir: Path | None = None,
    path_env: str | None = None,
    platform_name: str | None = None,
) -> None:
    """Reject mixed toolchains and missing extensions before any data mutation."""
    bindir = server.resolve().parent
    for name in ("postgres", "initdb", "pg_ctl", "pg_config", "pg_dump", "pg_restore"):
        tool = pg_tool(
            name,
            home=home,
            platform_bin_dir=platform_bin_dir,
            path_env=path_env,
        )
        if tool.resolve().parent != bindir or not os.access(tool, os.X_OK):
            raise RuntimeError(f"PostgreSQL tool {tool} does not belong to {bindir}")
        _require_pg17(tool)
    config = bindir / "pg_config"
    if Path(_output(config, "--bindir")).resolve() != bindir:
        raise RuntimeError(f"PostgreSQL pg_config does not describe {bindir}")
    extension = Path(_output(config, "--sharedir")) / "extension"
    library = Path(_output(config, "--pkglibdir"))
    on_macos = is_macos() if platform_name is None else platform_name == "darwin"
    suffix = ".dylib" if on_macos else ".so"
    if not (library / f"vector{suffix}").is_file() or not _has_vector_install_script(extension):
        raise RuntimeError(f"Install pgvector for the PostgreSQL 17 runtime at {bindir}")


def _has_vector_install_script(extension: Path) -> bool:
    """Packaged pgvector must carry the base script named by its control file."""
    control = extension / "vector.control"
    if not control.is_file():
        return False
    assignments = re.findall(r"^\s*default_version\s*=(.*)$", control.read_text(), re.MULTILINE)
    if len(assignments) != 1:
        return False
    version = re.fullmatch(r"\s*'([0-9]+(?:\.[0-9]+)+)'\s*(?:#.*)?", assignments[0])
    return version is not None and (extension / f"vector--{version[1]}.sql").is_file()


def ensure_pg_runtime(
    *,
    home: Path,
    system: str | None = None,
    machine: str | None = None,
    platform_bin_dir: Path | None = None,
    path_env: str | None = None,
    platform_name: str | None = None,
    pg_artifacts: Mapping[str, tuple[str, str]] | None = None,
    pgvector_artifacts: Mapping[str, tuple[str, str]] | None = None,
    open_url: UrlOpener | None = None,
) -> None:
    supported = (
        runtime_binaries.vendored_pg_supported()
        if (system, machine) == (None, None)
        else runtime_binaries.vendored_pg_supported(system=system, machine=machine)
    )
    if not supported:
        server = installed_server(platform_bin_dir=platform_bin_dir, path_env=path_env)
        if server is None:
            raise RuntimeError(
                "no vendored Postgres artifact for this platform and no installed "
                "PostgreSQL 17 found"
            )
        _verify_installed_runtime(
            server,
            home=home,
            platform_bin_dir=platform_bin_dir,
            path_env=path_env,
            platform_name=platform_name,
        )
        return
    runtime_binaries.ensure_pg_binaries(
        home=home,
        system=system,
        machine=machine,
        artifacts=pg_artifacts,
        open_url=open_url,
    )
    runtime_binaries.ensure_pgvector(
        home=home,
        system=system,
        machine=machine,
        artifacts=pgvector_artifacts,
        open_url=open_url,
    )
