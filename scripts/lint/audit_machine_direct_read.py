# meiva:note
# One read path for the audit machine dimension (task #6089).
#
# `audit_events.machine` is `{canonical name} | unknown` going forward and carries four
# historical hostnames behind it. Both facts live in one place: `audit_events_canonical` (the
# view) and `base/telemetry/machine_identity.py` (the mapping). A module that reads the column
# directly re-invents half of that knowledge and drifts -- which is exactly how one machine came
# to be counted as two.
#
# The rule: only the modules below may name `machine` next to `audit_events`. Everyone else
# reads the view.
#
# Usage: .venv/bin/python scripts/lint/audit_machine_direct_read.py
#   (the whole repository; the hook passes no file names, so a reviewer's subset cannot hide a
#   reader that appears later)

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN = ("base", "cli", "gateway", "agent", "services", "schedules", "db")

# meiva:note
# The files that may read the column, each for a reason that does not generalize; a new entry
# needs a reason that survives review, not a name.
ALLOWED = {
    "base/telemetry/audit_events.py": "writes and reads back the row it inserts",
    "base/telemetry/machine/identity.py": "owns the hostname-to-name mapping",
    "base/telemetry/delivery/state.py": "resolves the dimension before it is written",
    "base/agents/impersonation/history.py": "replays a recorded payload verbatim, not today's reading of it",
}

_READS_MACHINE = re.compile(r"\bselect\b[^;]{0,400}?\bmachine\b", re.IGNORECASE | re.DOTALL)


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for top in SCAN:
        files.extend(sorted((ROOT / top).rglob("*.py")))
    return files


def main() -> int:
    offenders: list[str] = []
    for path in _iter_files():
        rel = path.relative_to(ROOT).as_posix() if path.is_absolute() else path.as_posix()
        if rel in ALLOWED or "/tests/" in f"/{rel}" or rel.startswith("migrations/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for anchor in re.finditer(r"audit_events\b", text):
            window = text[max(0, anchor.start() - 400) : anchor.end() + 200]
            match = _READS_MACHINE.search(window)
            if match is None:
                continue
            snippet = " ".join(window[match.start() : match.end()].split())[:120]
            line = text[: anchor.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}: reads `machine` from audit_events directly: {snippet}")
            break
    if offenders:
        print("audit machine read must go through audit_events_canonical (task #6089):")
        for line in offenders:
            print(f"  {line}")
        return 1
    print("audit machine read: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
