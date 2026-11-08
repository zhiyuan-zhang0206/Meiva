# meiva:note
# Report the registry root this process computes: the probe `test_port_lease.py` runs against a
# second process that carries a different `TMPDIR`.
#
# A separate file rather than `python -c` because the claim is about what *another* process
# computes, and the caller chooses that process's environment.

from __future__ import annotations

import sys

from base.host.env.port_lease import registry_dir


def main() -> None:
    sys.stdout.write(str(registry_dir()) + "\n")


if __name__ == "__main__":
    main()
