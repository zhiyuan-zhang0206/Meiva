# meiva:note
# Child of `test_settings_values.py`: imports the settings readers in a fresh interpreter and
# reports whether the eager configuration chain was built.

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from cli.commands.observability import inputs

assert inputs.collector_settings_from_boot
assert "base.config" in sys.modules
assert "base.config._full" not in sys.modules
