# meiva:note
# Helper-spawned child: a bounded scan of a guarded tree (the fill trigger).
# `onboard-helper-grants.py --fill-pending` runs it through the permissions helper:
# the child touches a tree the helper is allowed to reach, which makes the OS ask
# the user for the folder grant. Arguments: the target path and the result path.

import sys
from dataclasses import dataclass
from pathlib import Path

MAX_DIRS = 64
MAX_READS = 32
READ_BYTES = 16


@dataclass
class _Walk:
    # meiva:note
    # The bounded walk's progress: directories entered, files sampled.

    visited: int = 0
    sampled: int = 0

    def scan(self, path: Path, depth: int) -> None:
        if self.visited >= MAX_DIRS:
            return
        self.visited += 1
        for entry in sorted(path.iterdir())[:MAX_DIRS]:
            if self.visited >= MAX_DIRS:
                return
            if entry.is_dir():
                if depth > 1:
                    self.scan(entry, depth - 1)
            elif self.sampled < MAX_READS:
                with entry.open("rb") as handle:
                    handle.read(READ_BYTES)
                self.sampled += 1


def main() -> None:
    target, result_path = Path(sys.argv[1]), Path(sys.argv[2])
    walk = _Walk()
    try:
        walk.scan(target, 2)
        text = f"granted dirs={walk.visited} files={walk.sampled}"
    except FileNotFoundError:
        text = f"missing {target}"
    except PermissionError as exc:
        text = f"denied {exc}"
    except OSError as exc:
        text = f"error {type(exc).__name__}: {exc}"
    result_path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
