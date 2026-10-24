# meiva:note
# Child entry: run the host-dependency gate on the host the parent names.
# The parent executes this file with an empty PATH and the platform as argv[2], so
# every probe meets a host where no external tool resolves — the "a required tool
# was removed from PATH" case — and the rows under test are the platform's own, not
# the ones this machine's `sys.platform` would pick. `Host.system()` would read the
# test machine; the `Host` built here is the host under test.

from __future__ import annotations

import sys
from pathlib import Path

from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86, fake_host
from cli.commands.data_plane import host_dependencies as hd

_PLATFORMS = {"linux": LINUX_X86, "darwin": DARWIN_ARM64}


def _no_install(_repo: Path, _component: str) -> None:
    return


def main() -> int:
    try:
        platform = _PLATFORMS[sys.argv[2]]
    except (IndexError, KeyError):
        raise SystemExit(f"usage: {Path(__file__).name} REPO PLATFORM (linux|darwin)") from None
    host = fake_host(platform=platform, uid=1000)
    scope = hd.StartScope(
        roles=frozenset({"agent-runner"}),
        services=frozenset({"gateway"}),
        data_plane=None,
        host=host,
        os_jobs=True,
        permissions_helper=False,
        require_github_pr=False,
        memory_keep_local=False,
        prod_install=True,
    )
    return hd.ensure_host_dependencies(repo=Path(sys.argv[1]), scope=scope, install=_no_install)


if __name__ == "__main__":
    raise SystemExit(main())
