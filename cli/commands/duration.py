from __future__ import annotations

import re

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


# meiva:note
# The one reader of the CLI's `<n><unit>` duration syntax (`30s` / `15m` / `24h` /
# `7d`, a bare number meaning seconds). Several flags accept it — a package's
# check interval, a skill install's interval and `meiva stop --maintenance-for` —
# so it lives at the input boundary they share rather than in any one command.
# meiva:link docs/conventions/python-conventions.md
def parse_duration(text: str) -> int:
    match = _DURATION_RE.match(text)
    if match is None:
        raise ValueError(f"duration {text!r} must look like 30s / 15m / 24h / 7d")
    return max(1, int(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"])
