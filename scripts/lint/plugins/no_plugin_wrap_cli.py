# meiva:note
# The command-line entry of the plugin-wrap rule: the rule decides, this shell reads the argument
# vector once and hands it down, so `scripts/lint/plugins/no_plugin_wrap.py` never reads the
# command line it was started with.

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(_REPO_ROOT))

from scripts.lint.plugins import no_plugin_wrap  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(no_plugin_wrap.main(sys.argv[1:]))
