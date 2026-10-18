from __future__ import annotations

import subprocess
from pathlib import Path

from base.host.proc import CommandRunner
from base.log import logger

_BREW_TIMEOUT_S = 60.0

# meiva:note
# PINNED_BREW_FORMULAE is the operator-approved Homebrew formula manifest; unpinned installed members are reported and uninstalled members are not.
# meiva:link base/host/docs/brew_pin.meiva.okf.md
PINNED_BREW_FORMULAE: frozenset[str] = frozenset(
    {
        "ca-certificates",
        "cloudflared",
        "grafana",
        "json-c",
        "node",
        "openssl@3",
        "pgbouncer",
        "pgvector",
        "postgresql@17",
        "redis",
        "redis@8.2",
        "uv",
    }
)

# meiva:link base/host/docs/brew_pin.meiva.okf.md
UV_VERSION = "0.13.1"

UV_ASSET_SHA256: dict[str, str] = {
    "aarch64-apple-darwin": "cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2",
    "x86_64-apple-darwin": "bb7b805bdcf3d0bc8c74ceb94569e6b3e8aa35bd492acdbb9d46bf46d9d4ff6b",
    "aarch64-unknown-linux-gnu": "9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14",
    "x86_64-unknown-linux-gnu": "eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220",
}

# meiva:link base/host/docs/brew_pin.meiva.okf.md
PGBOUNCER_APT_VERSION = "1.26.0-1.pgdg24.04+1"

REDIS_APT_VERSION = "6:8.2.*"


# meiva:note
# A failed `brew list` means "could not ask", never "nothing matched": the empty answer is the
# positive claim a clean pin set is read from, so it returns None (unknown) and reports instead.
# meiva:link base/host/docs/brew_pin.meiva.okf.md
def _brew_lines(args: list[str], *, run: CommandRunner, what: str) -> set[str] | None:
    try:
        result = run(["brew", *args], capture_output=True, text=True, timeout=_BREW_TIMEOUT_S)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "brew {what} could not run; the brew-pin assertion cannot judge this host: {err}",
            what=what,
            err=repr(exc),
            event="brew_pin",
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "brew {what} exited {rc}; the brew-pin assertion cannot judge this host: {err}",
            what=what,
            rc=result.returncode,
            err=(result.stderr or "").strip()[:400],
            event="brew_pin",
        )
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


# meiva:link base/host/docs/brew_pin.meiva.okf.md
def pinned_brew_formulae(*, run: CommandRunner) -> set[str] | None:
    return _brew_lines(["list", "--pinned"], run=run, what="list --pinned")


def installed_brew_formulae(*, run: CommandRunner) -> set[str] | None:
    return _brew_lines(["list", "--formula"], run=run, what="list --formula")


def unpinned_formulae(*, run: CommandRunner) -> tuple[str, ...] | None:
    pinned = pinned_brew_formulae(run=run)
    if pinned is None:
        return None
    installed = installed_brew_formulae(run=run)
    if installed is None:
        return None
    return tuple(sorted((PINNED_BREW_FORMULAE & installed) - pinned))


def brew_prefix(formula: str = "") -> Path:
    cmd = ["brew", "--prefix", *([formula] if formula else [])]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return Path("/opt/homebrew")
    return Path(out.stdout.strip() or "/opt/homebrew")
