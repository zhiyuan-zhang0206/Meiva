# meiva:note
# Run by the test as a child: import the updater's entry point, which loads every module of the
# package, and print which of the application's own packages that pulled in. A bare operator
# checkout has the repository on the path and nothing installed, so the window must not need any.
# meiva:link cli/docs/fleet-update.meiva.okf.md
from __future__ import annotations

import sys

from cli.fleet_update import __main__ as entry

_APPLICATION = {"base", "gateway", "services", "agent", "meiva"}

if __name__ == "__main__":
    modules = set(sys.modules)
    print(sorted({name.split(".")[0] for name in modules} & _APPLICATION))
    print(sorted(name for name in modules if name.startswith("cli.fleet_update")) != [])
    print(
        sorted(
            name
            for name in modules
            if name.startswith("cli.") and not name.startswith("cli.fleet_update")
        )
    )
    print(entry.__name__)
