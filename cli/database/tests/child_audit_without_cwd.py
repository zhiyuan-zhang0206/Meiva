# meiva:note
# A fresh process whose working directory is removed under it, running one audited command.
#
# argv[1] is the directory to create, enter and remove; argv[2] is `operator` for the audit scope
# of an operator command or `boot` for the OS boot job's retried start. The command body prints
# `command ran` and records the outcome, which is how a test sees that the audit scope did not
# stop it. The database factory refuses every call: an unaudited command must never reach it.

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from cli.database.lifecycle_audit import operator_lifecycle_audit, record_lifecycle_request


def _refuse_database() -> NoReturn:
    raise AssertionError("an unaudited command must not reach the database")


def main(argv: list[str]) -> int:
    gone = Path(argv[1])
    scope = argv[2]
    if scope not in ("operator", "boot"):
        raise ValueError(f"unknown audit scope {scope!r}: operator or boot")
    gone.mkdir()
    os.chdir(gone)
    gone.rmdir()
    with operator_lifecycle_audit(
        database_factory=_refuse_database, enabled=scope == "operator"
    ) as command:
        record_lifecycle_request("down", ["gateway"])
        print("command ran")
        command.exited(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
