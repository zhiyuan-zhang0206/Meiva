# meiva:note
# Lease ports in a fresh process and report them: the concurrency probe of
# `test_port_lease.py`.
#
# A separate file rather than `python -c` because a lease belongs to a process and the
# probe must be a process of its own. It waits for one line on stdin after reporting,
# so a caller can observe the registry while this holder is still alive; closing stdin
# (or `communicate()`) ends the holder immediately.

from __future__ import annotations

import json
import sys

from base.host.env.port_lease import lease_ports


def main(count: int) -> None:
    sys.stdout.write(json.dumps(lease_ports(count)) + "\n")
    sys.stdout.flush()
    sys.stdin.readline()


if __name__ == "__main__":
    main(int(sys.argv[1]))
