# meiva:note
# The command-line entry of the clock-lattice rule: the rule decides, this shell reads the roles the
# repository declares and hands the directories down, so `meizel/owners/clock_lattice.py` never reads
# a file to learn what it governs.

from __future__ import annotations

from pathlib import Path

from meizel.checkout import roles_at
from meizel.owners import clock_lattice
from meizel.roles import select_packages

SCAN_DIRS = select_packages(
    roles_at(Path(__file__).parents[2]), "application", "standalone", "tooling"
)

__all__ = ["SCAN_DIRS"]

if __name__ == "__main__":
    raise SystemExit(clock_lattice.main(scan_dirs=SCAN_DIRS))
