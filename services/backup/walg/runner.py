"""Run the pinned wal-g with this home's configuration.

One entry for every wal-g invocation: argv is `[wal-g, --config <file>, *args]`
and nothing else crosses into the child. The environment is the daemon subset
(`daemon_process_env`: PATH, HOME, locale; no `MEIVA_*` secret) plus, for commands
that talk to Postgres, the libpq variables that name this home's owner-only
socket and the OS-user administrator (peer authentication, no password). Storage
credentials stay in the 0600 file, never in argv or the environment.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, overload
from urllib.parse import parse_qs, urlsplit

from base.cluster.dataplane.walg_binary import walg_path
from base.native_process.child_env import filter_daemon_env
from services.backup.walg.config import WalgConfigError

# meiva:note
# No variable beyond the daemon subset: an immutable empty mapping, so "none" is a value.
_NO_EXTRA: Mapping[str, str] = MappingProxyType({})


class WalgCommandError(RuntimeError):
    """wal-g exited non-zero or timed out. Carries its stderr, never its environment."""


def _pg_env(admin_url: str) -> dict[str, str]:
    """libpq variables for `admin_url` (`postgresql://<user>@/postgres?host=<dir>&port=<n>`)."""
    parts = urlsplit(admin_url)
    query = parse_qs(parts.query)
    return {
        "PGHOST": query["host"][0],
        "PGPORT": query["port"][0],
        "PGUSER": parts.username or "",
        "PGDATABASE": parts.path.lstrip("/"),
    }


def walg_env(
    *,
    environ: Mapping[str, str],
    pg_admin_url: str | None = None,
    extra: Mapping[str, str] = _NO_EXTRA,
) -> dict[str, str]:
    env = filter_daemon_env(environ)
    if pg_admin_url is not None:
        env.update(_pg_env(pg_admin_url))
    env.update(extra)
    return env


@dataclass(frozen=True)
class WalgOutput:
    """What a successful call printed: JSON and tables on stdout, WAL-G's log on stderr."""

    stdout: str
    stderr: str


def run_walg_logged(
    args: Sequence[str],
    *,
    timeout_s: float,
    pg_admin_url: str | None = None,
    extra_env: Mapping[str, str] = _NO_EXTRA,
    path: Path | None,
    home: Path,
    environ: Mapping[str, str],
) -> WalgOutput:
    """Run `wal-g --config <file> *args` and return both streams.

    For the commands whose result is in WAL-G's log (`delete` lists what it would
    remove only there). Everything else uses `run_walg`.

    Raises:
        WalgConfigError: WAL-G is not configured.
        WalgCommandError: non-zero exit or timeout (the stderr tail is attached).
    """
    if path is None:
        raise WalgConfigError("MEIVA_WALG_CONFIG_FILE is not set")
    argv = [str(walg_path(home=home)), "--config", str(path), *args]
    try:
        result = subprocess.run(  # noqa: S603 — the pinned wal-g with a fixed argv shape
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=walg_env(environ=environ, pg_admin_url=pg_admin_url, extra=extra_env),
        )
    except subprocess.TimeoutExpired:
        raise WalgCommandError(f"wal-g {args[0]} did not finish within {timeout_s:g}s") from None
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-5:]
        raise WalgCommandError(
            f"wal-g {args[0]} failed (exit {result.returncode}): {' | '.join(tail)}"
        )
    return WalgOutput(stdout=result.stdout, stderr=result.stderr)


@overload
def run_walg(
    args: Sequence[str],
    *,
    timeout_s: float,
    pg_admin_url: str | None = None,
    extra_env: Mapping[str, str] = _NO_EXTRA,
    as_json: Literal[False] = False,
    path: Path | None,
    home: Path,
    environ: Mapping[str, str],
) -> str: ...


@overload
def run_walg(
    args: Sequence[str],
    *,
    timeout_s: float,
    pg_admin_url: str | None = None,
    extra_env: Mapping[str, str] = _NO_EXTRA,
    as_json: Literal[True],
    path: Path | None,
    home: Path,
    environ: Mapping[str, str],
) -> object: ...


# meiva:note
# Run `wal-g --config <file> *args`; stdout text, or parsed JSON with `as_json`.
# Raises WalgConfigError when WAL-G is not configured, or WalgCommandError on non-zero exit or timeout.
def run_walg(
    args: Sequence[str],
    *,
    timeout_s: float,
    pg_admin_url: str | None = None,
    extra_env: Mapping[str, str] = _NO_EXTRA,
    as_json: bool = False,
    path: Path | None,
    home: Path,
    environ: Mapping[str, str],
) -> object:
    output = run_walg_logged(
        args,
        timeout_s=timeout_s,
        pg_admin_url=pg_admin_url,
        extra_env=extra_env,
        path=path,
        home=home,
        environ=environ,
    )
    return json.loads(output.stdout) if as_json else output.stdout
