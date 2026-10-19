from __future__ import annotations

import json
import os
import shutil
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, cast

import psycopg

from base.api_contracts.agent_contract import (
    ForkHarnessMismatch,
    Harness,
    HarnessNotLaunchable,
    HarnessStatus,
)
from base.db import fetch_one

DEFAULT_HARNESS = Harness.MEIVA

# meiva:note
# A harness enters this set when a launcher that can start a session on it
# exists. Until then the runner refuses to wake a row born on it: recording an
# intent the platform cannot honour would make the stored value a claim about a
# runtime loop that is not actually running. Every external harness is here
# because the graph routes its turns to `harness_turn`, whose bridge opens an
# adapter session; removing a value retires its launcher and leaves rows born on
# it inert, which is the rollback.
# meiva:link base/agents/docs/harness.meiva.okf.md
LAUNCHABLE_HARNESSES: frozenset[Harness] = frozenset(
    {Harness.MEIVA, Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY}
)


class HarnessProbe(NamedTuple):
    harness: Harness
    status: HarnessStatus
    reason: str
    binary: str | None = None
    on_path: bool = False


@dataclass(frozen=True)
class _LoginEvidence:
    signed_in: bool | None
    detail: str


# meiva:note
# Install directories a user-level CLI lands in even when the process PATH is
# trimmed. Agents start without the interactive shell profile, so a missing PATH
# entry alone is not proof that the harness is absent.
# meiva:link base/agents/docs/harness.meiva.okf.md
_FALLBACK_BIN_DIRS = (
    ".local/bin",
    ".npm-global/bin",
    ".bun/bin",
    ".claude/local",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)


def coerce_harness(value: str | Harness | None) -> Harness:
    if value is None:
        return DEFAULT_HARNESS
    if isinstance(value, Harness):
        return value
    try:
        return Harness(value)
    except ValueError:
        legal = ", ".join(harness.value for harness in Harness)
        raise ValueError(f"unknown harness {value!r}; legal values: {legal}") from None


# meiva:note
# Birth is the last moment this choice is cheap to refuse: a stored harness is
# immutable, so a value no launcher can start would leave an agent that can
# neither run nor move onto another loop, and only a force terminate could end
# it. A fork inherits its source's harness, so a source that never got a
# launcher is refused by the same rule instead of copying the dead value.
# meiva:link base/agents/docs/harness.meiva.okf.md
def _require_launchable(
    harness: Harness, fork_from: int | None, launchable: Collection[Harness]
) -> Harness:
    if harness in launchable:
        return harness
    allowed = ", ".join(sorted(item.value for item in launchable))
    if fork_from is None:
        raise HarnessNotLaunchable(
            f"harness {harness.value!r} has no launcher yet: nothing in this release can run "
            f"an agent on it, and a stored harness never changes, so the row could never run. "
            f"Spawn on one of {allowed} instead"
        )
    raise HarnessNotLaunchable(
        f"cannot fork agent {fork_from}: it was born on harness {harness.value!r}, which has "
        f"no launcher yet, and a fork keeps its source's harness, so the child could never "
        f"run either. Fork an agent on one of {allowed}, or spawn a new one"
    )


# meiva:note
# A fork continues its source's identity, so it runs the source's harness. Naming
# a different one is refused here, inside the birth transaction, so a caller that
# skipped request validation still cannot fork across harnesses; omitting the
# value inherits the source's.
# meiva:link base/agents/docs/harness.meiva.okf.md
def resolve_birth_harness(
    cur: psycopg.Cursor[Any],
    requested: str | Harness | None,
    fork_from: int | None,
    *,
    launchable: Collection[Harness] = LAUNCHABLE_HARNESSES,
) -> str:
    if fork_from is None:
        return _require_launchable(coerce_harness(requested), fork_from, launchable).value
    cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (fork_from,))
    row = fetch_one(cur, "harness: read fork source")
    source = Harness(row[0])
    if requested is None:
        return _require_launchable(source, fork_from, launchable).value
    requested_harness = coerce_harness(requested)
    if requested_harness is not source:
        raise ForkHarnessMismatch(
            f"cannot fork agent {fork_from} onto harness {requested_harness.value!r}: the "
            f"source runs on {source.value!r}, and harness is an immutable property of an "
            f"agent. Spawn a new agent with harness={requested_harness.value!r} instead of "
            f"forking onto it."
        )
    return _require_launchable(source, fork_from, launchable).value


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _has_value(mapping: object, key: str) -> bool:
    if not isinstance(mapping, dict):
        return False
    return bool(cast("dict[str, object]", mapping).get(key))


def _claude_code_login(home: Path) -> _LoginEvidence:
    if (home / ".claude" / ".credentials.json").is_file():
        return _LoginEvidence(
            signed_in=True, detail="credentials file found at ~/.claude/.credentials.json"
        )
    account = _read_json(home / ".claude.json")
    if _has_value(account, "oauthAccount"):
        return _LoginEvidence(signed_in=True, detail="signed-in account recorded in ~/.claude.json")
    if (home / ".claude.json").is_file():
        return _LoginEvidence(signed_in=False, detail="~/.claude.json records no signed-in account")
    return _LoginEvidence(signed_in=None, detail="no Claude Code configuration found")


def _codex_login(home: Path) -> _LoginEvidence:
    auth = home / ".codex" / "auth.json"
    if not auth.is_file():
        return _LoginEvidence(signed_in=None, detail="no ~/.codex/auth.json")
    data = _read_json(auth)
    if data is None:
        return _LoginEvidence(signed_in=None, detail="~/.codex/auth.json is unreadable")
    if _has_value(data, "OPENAI_API_KEY") or _has_value(data, "tokens"):
        return _LoginEvidence(signed_in=True, detail="credentials found in ~/.codex/auth.json")
    return _LoginEvidence(signed_in=False, detail="~/.codex/auth.json holds no credentials")


def _antigravity_login(_home: Path) -> _LoginEvidence:
    return _LoginEvidence(
        signed_in=None, detail="the CLI stores its session outside its config directory"
    )


_LOGIN_CHECKS = {
    Harness.CLAUDE_CODE: _claude_code_login,
    Harness.CODEX: _codex_login,
    Harness.ANTIGRAVITY: _antigravity_login,
}

_BINARIES = {
    Harness.CLAUDE_CODE: "claude",
    Harness.CODEX: "codex",
    Harness.ANTIGRAVITY: "agy",
}

_UNPROBED = set(Harness) - {Harness.MEIVA} - set(_BINARIES) - set(_LOGIN_CHECKS)
if _UNPROBED:
    raise RuntimeError(
        "harnesses missing a binary or login probe entry: "
        f"{sorted(item.value for item in _UNPROBED)}"
    )


def _locate(
    binary: str, home: Path, path_env: str | None, bin_dirs: Sequence[str]
) -> tuple[str | None, bool]:
    found = shutil.which(binary, path=path_env)
    if found is not None:
        return str(Path(found).resolve()), True
    for entry in bin_dirs:
        candidate = Path(entry) if Path(entry).is_absolute() else home / entry
        path = candidate / binary
        if path.is_file() and os.access(path, os.X_OK):
            return str(path), False
    return None, False


def _probe_external(
    harness: Harness, home: Path, path_env: str | None, bin_dirs: Sequence[str]
) -> HarnessProbe:
    binary = _BINARIES[harness]
    path, on_path = _locate(binary, home, path_env, bin_dirs)
    if path is None:
        searched = ", ".join(
            entry if Path(entry).is_absolute() else f"~/{entry}" for entry in bin_dirs
        )
        return HarnessProbe(
            harness, "unavailable", f"the {binary} command was not found on PATH or in {searched}"
        )
    installed = f"installed at {path}" if on_path else f"installed at {path} (not on PATH)"
    evidence = _LOGIN_CHECKS[harness](home)
    if evidence.signed_in is True:
        return HarnessProbe(harness, "available", f"{installed}; {evidence.detail}", path, on_path)
    if evidence.signed_in is False:
        return HarnessProbe(
            harness,
            "unavailable",
            f"{installed}; not signed in: {evidence.detail}. Run {binary} once and sign in",
            path,
            on_path,
        )
    return HarnessProbe(
        harness,
        "unknown",
        f"{installed}; login state unknown: {evidence.detail}. "
        f"Run {binary} once in a terminal to confirm it is signed in",
        path,
        on_path,
    )


# meiva:note
# The probe reads the filesystem only: no command runs, so it cannot hang on a
# login prompt, spend quota or start a session. A sign-in it cannot prove is
# reported as unknown rather than guessed either way.
# meiva:link base/agents/docs/harness.meiva.okf.md
def probe_local_harnesses(
    home: Path | None = None,
    path_env: str | None = None,
    bin_dirs: Sequence[str] | None = None,
) -> list[HarnessProbe]:
    resolved_home = Path.home() if home is None else home
    resolved_bin_dirs = _FALLBACK_BIN_DIRS if bin_dirs is None else bin_dirs
    rows: list[HarnessProbe] = []
    for harness in Harness:
        if harness is Harness.MEIVA:
            rows.append(
                HarnessProbe(
                    harness, "available", "built-in runtime; needs no external CLI or login"
                )
            )
            continue
        rows.append(_probe_external(harness, resolved_home, path_env, resolved_bin_dirs))
    return rows
