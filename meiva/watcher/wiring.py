# meiva:note
# Build the `SpawnDeps` of this process: its agent, directories, sessions and live session ids.

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from base.host.env.dotenv_boot import watcher_runner_env
from base.paths import meiva_home, workspace_dir
from meiva.sdk_surface import agent_identity
from meiva.shell.background import cli_path
from meiva.shell.sessions import open_sessions, session_host
from meiva.watcher import files
from meiva.watcher.spawn import SpawnDeps

__all__ = ["spawn_deps"]


def spawn_deps() -> SpawnDeps:
    agent_id = agent_identity.require_agent_id()
    shell = open_sessions(agent_id)
    return SpawnDeps(
        agent_id=agent_id,
        watchers_dir=files.watchers_dir(meiva_home(), Path(tempfile.gettempdir()), agent_id),
        shell=shell,
        host=session_host(),
        workspace=workspace_dir(agent_id),
        cli=cli_path(),
        live_session_ids=frozenset(shell.list()),
        runner_env=watcher_runner_env(),
        python=sys.executable,
    )
