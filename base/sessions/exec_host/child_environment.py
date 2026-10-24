# meiva:note
# The environment contract shared by direct exec children and the exec-host service.
# The caller supplies the actual child interpreter's source root. A child outside that
# checkout, including a nested worktree, must not let bare uv target its editable venv.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base.host.env.agent_env import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV, AGENT_ID_ENV


@dataclass(frozen=True)
class ChildExecution:
    agent_id: int | None
    request_path: Path
    result_path: Path


def _cwd_is_inside_checkout(cwd: Path, checkout_root: Path) -> bool:
    return (
        cwd.is_relative_to(checkout_root)
        and not cwd.is_relative_to(checkout_root / ".worktrees")
        and not cwd.is_relative_to(checkout_root / ".claude" / "worktrees")
    )


# meiva:note
# Ambient agent config carriers never cross the boundary. Only the caller's explicit,
# non-empty per-agent maps, already serialized as JSON, bind the new execution.
def build_child_environment(
    environ: Mapping[str, str],
    *,
    cwd: Path,
    source_root: Path | None,
    execution: ChildExecution,
    config_overlay_json: str | None,
    birth_config_json: str | None,
) -> dict[str, str]:
    env = dict(environ)
    env.pop(AGENT_CONFIG_OVERLAY_ENV, None)
    env.pop(AGENT_BIRTH_CONFIG_ENV, None)
    if source_root is not None and not _cwd_is_inside_checkout(cwd, source_root):
        env.pop("VIRTUAL_ENV", None)
    if execution.agent_id is not None:
        env[AGENT_ID_ENV] = str(execution.agent_id)
    env["MEIVA_PROCESS_PROFILE"] = "agent"
    env["MEIVA_EXEC_REQUEST_FILE"] = str(execution.request_path)
    env["MEIVA_EXEC_RESULT_FILE"] = str(execution.result_path)
    if config_overlay_json is not None:
        env[AGENT_CONFIG_OVERLAY_ENV] = config_overlay_json
    if birth_config_json is not None:
        env[AGENT_BIRTH_CONFIG_ENV] = birth_config_json
    return env
