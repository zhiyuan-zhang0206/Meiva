# meiva:note
# A permission-denial probe owns only its child identity and fresh temporary directory.
# Imports finish before an optional root-to-unprivileged transition; the parent is untouched.
from __future__ import annotations

import errno
import os
import sys
import tempfile
from pathlib import Path

from meiva_builtins.plugins.meiva_memory.template import validate


def probe_uid(directory_owner: int) -> int:
    return 65534 if directory_owner == 0 else directory_owner


def _probe() -> int:
    with tempfile.TemporaryDirectory(prefix="budget-denial-") as name:
        root = Path(name)
        blocked = root / "memory/blocked"
        blocked.mkdir(parents=True)
        (blocked / "note.md").write_text("unreadable note", encoding="utf-8")
        blocked.chmod(0)
        try:
            try:
                _ = list(blocked.iterdir())
            except PermissionError as error:
                if error.errno != errno.EACCES:
                    raise RuntimeError(f"setup: unexpected denial errno {error.errno}") from error
            else:
                raise RuntimeError("setup: chmod did not deny directory access")
            try:
                validate.budget_snapshot(root, [])
            except PermissionError as error:
                if error.errno != errno.EACCES or error.filename != str(blocked):
                    raise RuntimeError(f"snapshot: unexpected denial source {error}") from error
                print("setup=denied snapshot=permission-error")
                return 0
            raise RuntimeError("snapshot: export succeeded despite unreadable subtree")
        finally:
            blocked.chmod(0o700)


def main(argv: list[str]) -> int:
    uid = int(argv[0])
    if uid <= 0:
        print("setup: a positive unprivileged UID is required", file=sys.stderr)
        return 2
    try:
        os.setuid(uid)
    except OSError as error:
        print(f"setup: could not select child UID: {error}", file=sys.stderr)
        return 2
    return _probe()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
