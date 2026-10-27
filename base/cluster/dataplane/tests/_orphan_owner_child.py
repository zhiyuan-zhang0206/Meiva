from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path

from base.cluster.dataplane import pg_tools
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.paths import meiva_home

if sys.argv[1] == "--hold-lock":
    fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, b'{"owner_pid": 0, "port": 0}')
    Path(sys.argv[3]).write_text("")
    time.sleep(600)
else:
    cm = pg_tools.throwaway_postgres(config=ThrowawayConfig(Path(sys.argv[1]), home=meiva_home()))
    url = cm.__enter__()
    target = Path(sys.argv[2])
    part = Path(sys.argv[2] + ".part")
    part.write_text(url)
    part.replace(target)
    time.sleep(600)
