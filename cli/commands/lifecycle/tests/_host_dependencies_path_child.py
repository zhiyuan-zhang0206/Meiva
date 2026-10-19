# meiva:note
# Child entry: run the host-dependency gate under the PATH the parent hands it.
# The parent test strips PATH and executes this file directly, so the gate meets a
# host where no external tool resolves — the "a required tool was removed from
# PATH" case, without patching this process's environment.

from __future__ import annotations

import sys
from pathlib import Path

from base.host.system.hostport import Host
from cli.commands.data_plane import host_dependencies as hd


def main() -> int:
    scope = hd.StartScope(
        roles=frozenset({"agent-runner"}),
        services=frozenset({"gateway"}),
        data_plane=None,
        host=Host.system(),
        os_jobs=True,
        permissions_helper=False,
    )
    return hd.ensure_host_dependencies(
        repo=Path(sys.argv[1]), scope=scope, install=lambda _repo, _component: False
    )


if __name__ == "__main__":
    raise SystemExit(main())
