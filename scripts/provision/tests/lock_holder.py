#!/usr/bin/env python
# meiva:note
# A process that takes the dpkg frontend lock and holds it until it is killed. Spawned by
# the apt-lock tests as the second process apt's collision is with — fcntl locks belong to
# a process, so a thread inside the test process could never play the holder.
# meiva:link scripts/provision/apt_lock.py

from __future__ import annotations

import fcntl
import os
import sys
import time


def main() -> int:
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.lockf(fd, fcntl.LOCK_EX)
    print("locked", flush=True)
    time.sleep(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
