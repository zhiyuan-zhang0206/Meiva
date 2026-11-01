from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from base.cluster.dataplane.runtime_binaries import (
    download,
    download_with_opener,
    runtime_root,
)
from base.host.net.ports import UrlOpener
from base.log import logger
from base.native_process.child_env import daemon_process_env

__all__ = [
    "WALG_ARTIFACTS",
    "WALG_VERSION",
    "ensure_walg_binary",
    "installed_problem",
    "walg_path",
]

WALG_VERSION = "v3.0.9"

_RELEASE_BASE = "https://github.com/wal-g/wal-g/releases/download"

WALG_ARTIFACTS: dict[str, tuple[str, str]] = {
    "linux-x86_64": (
        "wal-g-pg-24.04-amd64",
        "8015836f246a978f27b31a2462eac7765312558ae5b11ac033322b555abf9e22",
    ),
}

_VERSION_PROBE_TIMEOUT_S = 30


def _artifact(
    *,
    system: str,
    machine: str,
    artifacts: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    sys_name, arch = system, machine
    table = artifacts if artifacts is not None else WALG_ARTIFACTS
    if sys_name == "Linux" and arch in ("x86_64", "amd64"):
        return table["linux-x86_64"]
    raise RuntimeError(f"no pinned wal-g artifact for {sys_name.lower()}/{arch}")


# meiva:note
# The stable installed path, `<home>/runtime/walg/wal-g`. Pure: it never downloads.
def walg_path(home: Path) -> Path:
    return runtime_root(home) / "walg" / "wal-g"


def _sha256_of(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as binary:
            while block := binary.read(1 << 20):
                digest.update(block)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def installed_problem(
    *,
    home: Path,
    system: str,
    machine: str,
    artifacts: Mapping[str, tuple[str, str]] | None = None,
) -> str | None:
    try:
        asset, expected = _artifact(system=system, machine=machine, artifacts=artifacts)
    except RuntimeError as exc:
        return str(exc)
    path = walg_path(home)
    actual = _sha256_of(path)
    if actual is None:
        return f"wal-g is not installed at {path} (expected {asset} {WALG_VERSION})"
    if actual != expected:
        return f"wal-g at {path} is not the pinned {WALG_VERSION} build (sha256 {actual[:12]}…)"
    if not os.access(path, os.X_OK):
        return f"wal-g at {path} is not executable"
    return None


def _require_version(binary: Path) -> None:
    """The candidate binary must run and report the pinned version."""
    result = subprocess.run(  # noqa: S603 — the candidate is the checksum-verified pinned artifact
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=_VERSION_PROBE_TIMEOUT_S,
        env=daemon_process_env(),
    )
    if result.returncode != 0 or WALG_VERSION not in result.stdout:
        raise RuntimeError(
            f"downloaded wal-g does not report {WALG_VERSION} "
            f"(exit {result.returncode}: {result.stdout.strip()[:120]!r})"
        )


def ensure_walg_binary(
    *,
    home: Path,
    system: str,
    machine: str,
    artifacts: Mapping[str, tuple[str, str]] | None = None,
    open_url: UrlOpener | None = None,
) -> Path:
    asset, expected = _artifact(system=system, machine=machine, artifacts=artifacts)
    target = walg_path(home)
    if installed_problem(home=home, system=system, machine=machine, artifacts=artifacts) is None:
        return target

    url = f"{_RELEASE_BASE}/{WALG_VERSION}/{asset}"
    logger.info(f"[runtime] fetching wal-g {WALG_VERSION} from {url}")
    blob = (
        download_with_opener(url, open_url=open_url, what="wal-g")
        if open_url is not None
        else download(url, what="wal-g")
    )
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"wal-g artifact sha256 mismatch for {asset} {WALG_VERSION}: "
            f"expected {expected}, got {actual}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(dir=target.parent, prefix=".wal-g.", suffix=".tmp")
    candidate = Path(staged)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(blob)
            file.flush()
            os.fsync(file.fileno())
        candidate.chmod(0o755)
        _require_version(candidate)
        candidate.replace(target)
    finally:
        with suppress(FileNotFoundError):
            candidate.unlink()
    logger.info(f"[runtime] wal-g {WALG_VERSION} ready at {target}")
    return target
