# meiva:note
# A fresh operator owner reads its own home and configuration; only the process entry is
# exercised here, so nothing in the command is replaced. The exit code is the process's own, the
# receipt goes to stderr, and stdout carries the document a reader would parse.
from __future__ import annotations

import json
import sys

from base.paths import meiva_home
from cli.commands.jobs.heartbeats import cmd_jobs_heartbeats
from cli.database.runtime import CommandRuntime


def main() -> int:
    runtime = CommandRuntime.create(meiva_home())
    rc = cmd_jobs_heartbeats(runtime=runtime)
    print(json.dumps({"rc": rc}), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
