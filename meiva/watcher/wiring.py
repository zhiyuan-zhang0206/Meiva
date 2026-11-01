# meiva:note
# Build the `SpawnDeps` of this process: its agent, directories, sessions, CLI, runner env and
# interpreter. `runner_env` is the launcher's data-plane projection, an optional override; the
# machine token itself rides the pty session service's own environment (task #5979), so a
# launcher without a projection still yields a watcher that can send.
# This is the one place the watcher reads the OS user's home, and the environment it reads is the
# one the bound context carries rather than `os.environ` a second time: the launcher's runner
# projection is computed from that mapping here and travels down as a value. It runs on each
# launch, cron or at call, so the environment is read afresh every time.

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import MappingProxyType

from base.host.env.dotenv_boot import project_watcher_runner_env
from base.paths import meiva_home, workspace_dir
from meiva.sdk_surface import agent_identity
from meiva.shell.background import cli_path
from meiva.shell.sessions import open_sessions, process_environ, session_host
from meiva.watcher import files
from meiva.watcher.spawn import SpawnDeps

__all__ = ["spawn_deps"]


def spawn_deps() -> SpawnDeps:
    agent_id = agent_identity.require_agent_id()
    shell = open_sessions(agent_id)
    home = meiva_home()
    environ = process_environ()
    return SpawnDeps(
        agent_id=agent_id,
        watchers_dir=files.watchers_dir(home, Path(tempfile.gettempdir()), agent_id),
        shell=shell,
        host=session_host(environ),
        workspace=workspace_dir(agent_id, home=home),
        cli=cli_path(),
        runner_env=MappingProxyType(
            project_watcher_runner_env(environ, home=home, user_home=Path.home())
        ),
        python=sys.executable,
    )
