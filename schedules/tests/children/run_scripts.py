# meiva:note
# Child process for the script-entry test: run a schedule script's module body the way the
# runner does and report whether it got through.
#
# `imports PATH...` runs each script under a name other than `__main__`, so no schedule starts:
# the result per path says whether its imports resolved. `main PATH` runs one script as
# `__main__` with the runner's argv (just the script's own path) and does not return while the
# schedule's loop waits; an assembly error is raised. Both modes run in isolated mode with the
# script's own directory as the working directory, so only the installed module roots can
# satisfy an import.

from __future__ import annotations

import json
import runpy
import sys


def run_imports(paths: list[str]) -> None:
    outcome: dict[str, str] = {}
    for path in paths:
        try:
            runpy.run_path(path, run_name="materialized")
            outcome[path] = "ok"
        except Exception as exc:
            outcome[path] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write(json.dumps(outcome))


def run_main(path: str) -> None:
    del sys.argv[:]
    sys.argv.append(path)
    runpy.run_path(path, run_name="__main__")


if __name__ == "__main__":
    if sys.argv[1] == "imports":
        run_imports(sys.argv[2:])
    else:
        run_main(sys.argv[2])
