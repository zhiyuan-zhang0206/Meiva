from __future__ import annotations

import stat
from pathlib import Path

from langgraph.runtime import Runtime

from agent.hooks import Hook
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.log import logger
from base.paths import workspace_dir, workspaces_root


def _logical_cwd_error(cwd: Path) -> OSError | None:
    try:
        mode = cwd.stat().st_mode
    except OSError as exc:
        return exc
    if not stat.S_ISDIR(mode):
        return NotADirectoryError(f"persisted meiva.cwd is not a directory: {cwd}")
    return None


def _foreign_workspace(cwd: Path, agent_id: int, root: Path) -> str | None:
    try:
        relative = cwd.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    owner = relative.parts[0]
    return owner if owner.isdigit() and owner != str(agent_id) else None


def _cwd_fallback(context: MeivaContext, aid: int | None, home: Path) -> str:
    if aid is None:
        return str(context.user_home if context.user_home is not None else home)
    return str(workspace_dir(aid, home=home))


def _log_cwd_repair(cwd: Path, fallback: str, exc: OSError | None, owner: str | None) -> None:
    if exc is not None:
        logger.warning(
            "after_init: persisted cwd {cwd!r} failed validation ({exc!r}), "
            "falling back to {fallback!r} — state updated so future restarts use the new cwd",
            cwd=str(cwd),
            exc=exc,
            fallback=fallback,
        )
    elif owner is not None:
        logger.warning(
            "after_init: inherited cwd {cwd!r} belongs to agent {owner}'s workspace, "
            "re-homing to {fallback!r}",
            cwd=str(cwd),
            owner=owner,
            fallback=fallback,
        )


# meiva:note
# After initialization, a valid persisted logical cwd is preserved, except a cwd inside another agent's workspace (a fork inherits the source's cwd channel): that one is replaced by the invoking agent's workspace, because a workspace is agent identity rather than project context. An absent or unusable cwd is likewise replaced by the invoking agent's workspace, or home when no agent identity exists. The workspaces root comes from `base.paths.workspaces_root()`, its single owner. This does not change the Python process cwd.
# meiva:link agent/hooks/docs/hooks.meiva.okf.md
class ValidateCwdAfterInitHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: object,
        /,
    ) -> dict[str, object] | None:
        home = runtime.context.meiva_home
        if home is None:
            return None
        aid = None if runtime.context.identity is None else runtime.context.identity.agent_id
        if "cwd" not in state.model_fields_set or not state.cwd:
            return {"cwd": _cwd_fallback(runtime.context, aid, home)}
        cwd = Path(state.cwd)
        exc = _logical_cwd_error(cwd)
        owner = (
            _foreign_workspace(cwd, aid, workspaces_root(home=home))
            if (exc is None and aid is not None)
            else None
        )
        if exc is None and owner is None:
            return None
        fallback = _cwd_fallback(runtime.context, aid, home)
        _log_cwd_repair(cwd, fallback, exc, owner)
        return {"cwd": fallback}
