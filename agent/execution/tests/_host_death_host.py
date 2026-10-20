# meiva:note
# A stand-in host: ``_host_death_host.py OUT_FILE`` launches the exec child, prints its pid, and sleeps.

import subprocess
import sys
import time
from pathlib import Path

from base.native_process.exec_domain import ExecProcessDomain
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL

proc, _domain = ExecProcessDomain.launch_posix(
    [
        sys.executable,
        "-I",
        str(Path(__file__).with_name("_host_death_child.py")),
        "leader",
        sys.argv[1],
    ],
    process_control=SYSTEM_PROCESS_CONTROL,
    stdin=subprocess.PIPE,
)
sys.stdout.write(f"{proc.pid}\n")
sys.stdout.flush()
time.sleep(60)
