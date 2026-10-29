# meiva:note
# The rule's door: it puts the checkout root on the path (pre-commit runs this
# as a script path, where neither `meizel` nor `scripts` is importable) and hands
# out `main`. What each part owns is in the node below.
# meiva:link scripts/lint/docs/time-bomb-rule-parts.meiva.okf.md


from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lint.diagnostics.time_bomb_rule.entry import main  # noqa: E402

__all__ = ["main"]
