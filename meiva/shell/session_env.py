# meiva:note
# The environment a new shell session starts with, as a function of the machine facts a root read once.
# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base.host.env.agent_env import AGENT_ID_ENV


# meiva:note
# What the machine contributes to a new session: the checkout whose virtualenv a session inside it may activate, and the environment a session forwards (the registry's session view with the venv activation, `VIRTUAL_ENV` included). A root reads both once.
@dataclass(frozen=True)
class SessionHost:
    checkout: Path
    forwarded: Mapping[str, str]


# meiva:note
# The environment handed to one new session. A session outside the checkout keeps the venv-prefixed PATH but drops `VIRTUAL_ENV`, so a bare uv command cannot select this checkout's venv. The owner's id rides the environment so a script run in the session builds the owner's full `MeivaContext` (`meiva.boot.launched_context`, `owns_loop=False`; ruling 2026-10-04, docs/decisions/agents/context/2026-10-04-meiva-state-and-context.md, source 2). It is set per creation from the session's owner, not copied from the creating process, whose value the allowlist drops. The overrides (a watcher's runner credentials) win.
def session_environment(
    host: SessionHost,
    *,
    activate_venv: bool,
    agent_id: int,
    overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    env = dict(host.forwarded)
    if not activate_venv:
        env.pop("VIRTUAL_ENV", None)
    env.setdefault("TERM", "xterm-256color")
    env[AGENT_ID_ENV] = str(agent_id)
    if overrides:
        env.update(overrides)
    return env
