# meiva:note
# A model checker with a real child: record the child's native birth before waiting, so the
# timeout test proves that the checker and its descendant stop together.

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import psutil

from base.native_process.ownership import OwnedProcess


if __name__ == "__main__":
    with subprocess.Popen(
        ["/bin/sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as child:
        owner = OwnedProcess.capture(psutil.Process(child.pid))
        Path(sys.argv[1]).write_text(json.dumps(asdict(owner)))
        child.wait()
