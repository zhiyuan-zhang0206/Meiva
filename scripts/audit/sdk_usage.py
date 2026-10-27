# meiva:note
# Rank real SDK calls from JSONL event mirrors and suggest a stable expansion list.
#
# Read-only: this script never changes cluster configuration. Copy the desired event mirrors from each
# runner, then pass an explicit timezone-aware window. The decisions are in `sdk_usage_report.py`; this
# file is the command line: it reads the mirrors the operator names and prints the report.

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.audit.sdk_usage_report import read_calls, report, timestamp  # noqa: E402

_DESCRIPTION = (
    "Rank real SDK calls from JSONL event mirrors and suggest a stable expansion list. "
    "Read-only: this script never changes cluster configuration. Copy the desired event mirrors "
    "from each runner, then pass an explicit timezone-aware window."
)


# meiva:note
# One mirror's lines, read one at a time and closed when the last is taken.
def _lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as stream:
        yield from stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_DESCRIPTION)
    parser.add_argument("events", nargs="+", type=Path, help="JSONL event mirror files")
    parser.add_argument("--start", required=True, type=timestamp)
    parser.add_argument("--end", required=True, type=timestamp)
    parser.add_argument("--coverage", type=float, default=70, help="cumulative call percentage")
    parser.add_argument("--agent-id", type=int, action="append", help="repeat to narrow callers")
    args = parser.parse_args(argv)
    try:
        mirrors = [(str(path), _lines(path)) for path in args.events]
        data = report(read_calls(mirrors), args.start, args.end, args.coverage, args.agent_id)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
