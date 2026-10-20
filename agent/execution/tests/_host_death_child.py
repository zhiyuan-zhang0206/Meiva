# meiva:note
# An exec child that arms the host-death watch: ``_host_death_child.py leader OUT_FILE``.
#
# The leader writes ``<member pid> <repr of what it reads from stdin>`` to OUT_FILE, then sleeps
# together with one member of its process group.

import pathlib
import subprocess
import sys
import time

from agent.execution import child

if sys.argv[1] == "member":
    time.sleep(60)
else:
    child.die_with_host()
    member = subprocess.Popen([sys.executable, "-I", __file__, "member"])  # noqa: S603
    pathlib.Path(sys.argv[2]).write_text(f"{member.pid} {sys.stdin.read()!r}")
    time.sleep(60)
