from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import base.paths
import meiva
import meiva.files as _meiva_files
from base.agents.to_user.models import DOCUMENT_FILENAME
from base.host.boundary.untyped import object_of
from base.log import logger
from meiva.sdk_surface import process_context
from meiva.sdk_surface.agent_identity import require_agent_id
from meiva.sdk_surface.validation import coerce_str


def _absolute(path: str | Path) -> Path:
    target = Path(coerce_str(path, "path", allow_types=(os.PathLike,))).expanduser()
    if target.is_absolute():
        return target
    # meiva:note
    # `meiva.cwd` exists only while the meiva_code plugin is enabled, so it is looked up by name and
    # used when it answers with a path; without it the file namespace resolves the path.
    current = object_of(getattr, object_of(getattr, meiva, "cwd", None), "get", None)
    if callable(current):
        base_dir = current()
        if isinstance(base_dir, Path):
            return base_dir / target
    return _meiva_files.resolve(target)


# meiva:link meiva_builtins/plugins/meiva_to_user/docs/panel-write-check.meiva.okf.md
def panel_path(path: str | Path, *, agent_id: int, home: Path) -> Path | None:
    target = _absolute(path).resolve()
    if target.name != DOCUMENT_FILENAME:
        return None
    if target != (base.paths.workspace_dir(agent_id, home=home) / DOCUMENT_FILENAME).resolve():
        return None
    return target


class _StateHandle(Protocol):
    def update(self, delta: Mapping[str, object]) -> object: ...


def _state_handle() -> _StateHandle:
    materialize = object_of(getattr, object_of(getattr, meiva, "state"), "materialize", None)
    if callable(materialize):
        materialize()
    from .agent_runtime import state_handle

    return state_handle()


def record_panel_write(path: str | Path) -> None:
    try:
        if not process_context.in_exec_turn():
            return
        home = process_context.current_context().require_meiva_home()
        if panel_path(path, agent_id=require_agent_id(), home=home) is None:
            return
        _state_handle().update({"panel_written": True})
    except Exception:
        logger.opt(exception=True).warning(
            "to_user soft limit: could not record the write to {}; the panel goes unchecked",
            path,
        )
