# meiva:note
# The child of the stall-guard tests: ``stall_child.py noop`` exits, ``stall_child.py sleep`` sleeps
# two seconds, ``stall_child.py latch FD`` sends one ready byte on the inherited socket and exits when
# the parent shuts its side down. It imports nothing heavy, so it starts in a few milliseconds.

import socket
import sys
import time

if sys.argv[1] == "latch":
    with socket.socket(fileno=int(sys.argv[2])) as latch:
        latch.sendall(b"R")
        assert latch.recv(1) == b""
elif sys.argv[1] == "sleep":
    time.sleep(2)
elif sys.argv[1] != "noop":
    raise ValueError(f"unknown child mode {sys.argv[1]!r}")
