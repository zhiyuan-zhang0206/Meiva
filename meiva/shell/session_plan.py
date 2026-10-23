# meiva:note
# What a shell session may be called and live for, and how session ids map onto the names the backend lists, as functions of values. `ShellSessions` reads the backend and the database and asks these what the answers mean.
# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

import math
import re
from collections.abc import Sequence

# meiva:note
# A session name is a lowercase slug starting with a letter: it rides inside the session identifier (after the numeric id), so the charset stays conservative and the leading letter keeps it visually distinct from the id it follows.
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")

# meiva:note
# 24 hours: the cap on a user-facing session (ruling 2026-09-01). A system-side session, a watcher spawn, is exempt because its ttl is the watcher's true target deadline (ruling 2026-09-14, task #3411).
MAX_TTL_SECONDS = 86_400


# meiva:note
# A non-finite or non-positive ttl is refused on every path: a session whose deadline has already passed must never be created. The 24-hour cap protects user calls only; `system=True` marks a watcher spawn.
def validate_ttl(ttl: float, *, system: bool = False) -> float:
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("ttl must be finite and greater than zero")
    if not system and ttl > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl must be at most {MAX_TTL_SECONDS} seconds (24 hours) — "
            "sessions live at most one day"
        )
    return ttl


# meiva:note
# The `page-` label names `meiva.ui.serve` page sessions exactly (base/sessions/page_session.py owns the grammar, the caller asks it), which a terminate's shell kill spares, so an ordinary shell may not take it.
def validate_name(name: str | None, *, page_label: bool) -> None:
    if name is not None and not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"session name {name!r} invalid — use a lowercase slug like 'dev-server' "
            "([a-z][a-z0-9-]*)"
        )
    if page_label:
        raise ValueError(f"session name {name!r} invalid — 'page-' names are reserved for pages")


def session_full_name(shell_prefix: str, session_id: int, name: str | None) -> str:
    return f"{shell_prefix}{session_id}" + (f"-{name}" if name is not None else "")


# meiva:note
# The backend answers for every session it holds; this agent's are the ones under its shell prefix.
def own_names(names: Sequence[str], shell_prefix: str) -> list[str]:
    return [name for name in names if name.startswith(shell_prefix)]


# meiva:note
# The id is unique within the agent, so `…-shell-<id>` matches exactly one session whether or not it carries a `-<name>` suffix. Matching `<base>-` cannot conflate ids: `…-shell-12` does not start with `…-shell-1-`.
def match_session(names: Sequence[str], shell_prefix: str, session_id: int) -> str:
    base = f"{shell_prefix}{session_id}"
    for name in own_names(names, shell_prefix):
        if name == base or name.startswith(f"{base}-"):
            return name
    raise ValueError(f"session {session_id} is not this agent's (no match for {base!r})")


# meiva:note
# Each own session as `<id>` or `<id>-<name>` after the prefix: id to display name, None for an unnamed one.
def session_table(names: Sequence[str], shell_prefix: str) -> dict[int, str | None]:
    table: dict[int, str | None] = {}
    for full in own_names(names, shell_prefix):
        rest = full[len(shell_prefix) :]
        session_id, _, name = rest.partition("-")
        table[int(session_id)] = name or None
    return table
