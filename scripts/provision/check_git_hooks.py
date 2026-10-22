# meiva:note
# Warn about missing Git hooks or ephemeral interpreter pointers; never install.

# meiva:note
# `language: system` runs this under whatever `python3` the committer's PATH
# resolves to — the macOS system one is still 3.9, so keep `annotations` and
# insert the repository root before importing the stdlib-only checker.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from base.deploy.git.check_git_hooks import (
    git,
    hook_problem,
    hooks_path_override,
    inspect_checkout,
    machine_checkouts,
    main,
    scan_machine,
)

__all__ = [
    "git",
    "hook_problem",
    "hooks_path_override",
    "inspect_checkout",
    "machine_checkouts",
    "main",
    "scan_machine",
]

if __name__ == "__main__":
    sys.exit(main())
