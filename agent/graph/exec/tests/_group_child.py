# meiva:note
# A process-group leader with one sleeping member: ``_group_child.py PID_FILE`` writes the member's pid.

import pathlib
import subprocess
import sys
import time

if sys.argv[1] == "member":
    time.sleep(60)
else:
    member = subprocess.Popen([sys.executable, "-I", __file__, "member"])  # noqa: S603
    pathlib.Path(sys.argv[1]).write_text(str(member.pid))
    time.sleep(60)
