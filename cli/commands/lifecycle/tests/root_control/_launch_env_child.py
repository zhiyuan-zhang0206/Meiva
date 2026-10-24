# meiva:note
# Child entry: what the launcher hands the root, computed under the environment the parent
# gives this process. The root's env is built from the launcher's own environment, so a test
# controls it by running the builder in a process that has exactly the environment under test.
#
# usage: _launch_env_child.py env SERVICE_PATH
#        _launch_env_child.py digest SERVICE_PATH REPO HOME
#        _launch_env_child.py undeclared SERVICE_PATH

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from base.host.env.registry import HOST_PASSTHROUGH_KEYS, NETWORK_PROXY_KEYS, launch_input_keys
from base.sessions.env_forwarding import managed_service_env
from cli.commands.lifecycle.root_control.generation import launch_digest

_TEMP_DIR_KEYS = frozenset({"TMPDIR", "TEMP", "TMP"})
_AMBIENT = HOST_PASSTHROUGH_KEYS | NETWORK_PROXY_KEYS | _TEMP_DIR_KEYS | {"PYTHONUTF8"}


def main() -> int:
    mode, service_path = sys.argv[1], sys.argv[2]
    env = managed_service_env(service_path, environ=dict(os.environ))
    if mode == "env":
        print(json.dumps(env, sort_keys=True))
    elif mode == "digest":
        print(launch_digest(Path(sys.argv[3]), env, home=Path(sys.argv[4])))
    elif mode == "undeclared":
        print(json.dumps(sorted(set(env) - launch_input_keys() - _AMBIENT)))
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
