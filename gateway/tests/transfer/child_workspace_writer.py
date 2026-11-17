from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

_ = signal.signal(signal.SIGHUP, signal.SIG_IGN)
_ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
counter = Path(sys.argv[1])
staged = counter.with_name(counter.name + ".next")
_ = Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
value = 0
while True:
    value += 1
    _ = staged.write_text(str(value), encoding="utf-8")
    _ = staged.replace(counter)
    time.sleep(0.05)
