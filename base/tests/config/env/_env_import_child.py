# meiva:note
# Fresh-interpreter env/config import probes; never imported by a production entry.
#
# Run as ``python [-I] _env_import_child.py <scenario>`` with the environment prepared by the test.

from __future__ import annotations

import sys


def forwarded_timezone() -> None:
    from base.config import settings

    sys.stdout.write(f"{settings.general.timezone}\n")


def registry_imports_first() -> None:
    import base.host.env.registry

    _ = base.host.env.registry
    import base.config

    _ = base.config
    sys.stdout.write("ok\n")


SCENARIOS = {
    "forwarded_timezone": forwarded_timezone,
    "registry_imports_first": registry_imports_first,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
