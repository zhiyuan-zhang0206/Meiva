# meiva:note
# The `ssh` of a test that wants the host's own shell: it ignores the alias and runs the command
# the window sent in `FLEET_LOCAL_SHELL` (`zsh` or `sh`) with `FLEET_LOCAL_HOME` as its home, so
# the real commands of a window meet a real shell without a second machine. Standard library only.
# meiva:link cli/tests/fleet_update/docs/fleet-update-harness.meiva.okf.md

from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list[str]) -> int:
    command = argv[-1]
    env = {**os.environ, "HOME": os.environ["FLEET_LOCAL_HOME"]}
    done = subprocess.run(
        [os.environ["FLEET_LOCAL_SHELL"], "-c", command],
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return done.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
