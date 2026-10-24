# meiva:note
# A `uv` for the MCP install tests: a real program the build runs as a child process.
#
# The script stands in for `uv sync` inside a package directory. It leaves the `.venv/bin/python`
# a real build would, fails with the text it was given, and appends the lock-policy knobs it sees
# to a log file, so a test reads from the child what the build handed it. The host is the real
# `run_bounded` over a program table that finds only this `uv`.

from __future__ import annotations

import os
import shutil
from pathlib import Path

from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import LINUX_X86, fake_host
from cli.commands.extensions.mcp import UvBuild

__all__ = ["fake_uv_build", "real_uv_build"]

_SCRIPT = """#!/bin/sh
[ "$1" = sync ] || exit 2
[ -n "$FAKE_UV_LOG" ] && printf 'frozen=%s locked=%s\\n' "${UV_FROZEN-unset}" "${UV_LOCKED-unset}" >> "$FAKE_UV_LOG"
if [ -n "$FAKE_UV_FAIL" ]; then
  printf '%s\\n' "$FAKE_UV_FAIL" >&2
  exit 1
fi
mkdir -p .venv/bin
printf '#!/bin/sh\\n' > .venv/bin/python
chmod 755 .venv/bin/python
"""

_PATH = "/usr/bin:/bin"


# meiva:note
# The build whose `uv` is this script (written under `directory`). `fail_with` makes the build
# exit 1 saying it, `log` is where the script records the lock-policy knobs it was started with,
# and `caller_environ` is what the caller runs in (the script sees it minus the knobs).
def fake_uv_build(
    directory: Path,
    *,
    fail_with: str | None = None,
    log: Path | None = None,
    **caller_environ: str,
) -> UvBuild:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "uv"
    script.write_text(_SCRIPT)
    script.chmod(0o755)
    environ = {"PATH": _PATH, **caller_environ}
    if fail_with is not None:
        environ["FAKE_UV_FAIL"] = fail_with
    if log is not None:
        environ["FAKE_UV_LOG"] = str(log)
    return UvBuild(
        host=fake_host(run_bounded, programs={"uv": str(script)}), caller_environ=environ
    )


# meiva:note
# The build whose `uv` is the machine's: this host's own program lookup and process environment,
# for the two tests that build a real venv.
def real_uv_build(caller_environ: dict[str, str]) -> UvBuild:
    return UvBuild(
        host=Host(run=run_bounded, which=shutil.which, platform=LINUX_X86, uid=os.getuid()),
        caller_environ=caller_environ,
    )
