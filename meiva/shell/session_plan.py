# meiva:note
# What a shell session may be called and live for, and how session ids map onto the names the backend lists, as functions of values. `ShellSessions` reads the backend and the database and asks these what the answers mean.
# meiva:link meiva/shell/docs/shell.md
from __future__ import annotations

import math
import re
from collections.abc import Sequence

# meiva:note
# A session name is a lowercase slug starting with a letter: it rides inside the session identifier (after the numeric id), so the charset stays conservative and the leading letter keeps it visually distinct from the id it follows.
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")

# meiva:note
# 24 hours: the cap on a user-facing session (ruling 2026-09-01).
MAX_TTL_SECONDS = 86_400

# meiva:note
# 72 hours: the absolute cap for every new or renewed persistent shell, including
# system-owned watcher sessions. Existing rows can carry an older, longer deadline;
# readers intentionally accept those rows until their natural cleanup.
MAX_SYSTEM_TTL_SECONDS = 259_200


# meiva:note
# A non-finite or non-positive ttl is refused on every path: a session whose deadline has already passed must never be created. User sessions stay capped at 24 hours; `system=True` marks a watcher spawn, which may use up to the fleet-wide 72-hour absolute cap.
def validate_ttl(ttl: float, *, system: bool = False) -> float:
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("ttl must be finite and greater than zero")
    max_ttl = MAX_SYSTEM_TTL_SECONDS if system else MAX_TTL_SECONDS
    if ttl > max_ttl:
        hours = max_ttl // 3600
        raise ValueError(f"ttl must be at most {max_ttl} seconds ({hours} hours)")
    return ttl


def validate_name(name: str | None) -> None:
    if name is not None and not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"session name {name!r} invalid — use a lowercase slug like 'dev-server' "
            "([a-z][a-z0-9-]*)"
        )


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
