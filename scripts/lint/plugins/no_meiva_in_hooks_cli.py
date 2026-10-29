# meiva:note
# The command-line entry of the plugin-hook rule: the rule decides, this shell reads the roles the
# root manifest declares once and hands the scan directories down, so
# `scripts/lint/plugins/no_meiva_in_hooks.py` never reads a file to learn what it governs.
# meiva:link scripts/lint/docs/no-meiva-in-hooks.meiva.okf.md

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(_REPO_ROOT))

# meiva:link scripts/docs/scripts.meiva.okf.md
from meizel.checkout import roles_at  # noqa: E402
from meizel.roles import select_packages  # noqa: E402
from scripts.lint.plugins import no_meiva_in_hooks  # noqa: E402

SCAN_DIRS = select_packages(
    roles_at(_REPO_ROOT), "application", "standalone", "templates", "examples"
)

__all__ = ["SCAN_DIRS"]

if __name__ == "__main__":
    raise SystemExit(no_meiva_in_hooks.main(scan_dirs=SCAN_DIRS))
