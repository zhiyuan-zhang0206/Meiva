# meiva:note
# Report a home's serving gate from a process that owns no root: the reader the cross-process case
# launches, so both production readers -- the serving check and the recovery permission -- answer
# from the same client any other process uses.

from __future__ import annotations

import sys
from pathlib import Path

from base.deploy.lifecycle import start_serving


def main(report: Path, home: Path) -> None:
    with start_serving.recovery_permitted(home) as recovery:
        permitted = recovery
    report.write_text(
        f"serving={start_serving.is_serving(home)}\nrecovery={permitted}\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
