# meiva:note
# A program standing in for a scheduled scan or checker: it prints what it is told, exits with
# the code it is told, and can sleep first so a timeout has something to cut short.
#
# `--stdout TEXT`, `--stderr TEXT`, `--exit CODE` and `--sleep SECONDS`; with `--write-report DIR`
# and `--report-json TEXT` it leaves `DIR/last-report.json` behind, as the model checker does.

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days")
    parser.add_argument("--write-report")
    parser.add_argument("--stdout", default="")
    parser.add_argument("--stderr", default="")
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--report-json", default=None)
    args = parser.parse_args(argv)
    time.sleep(args.sleep)
    if args.write_report is not None and args.report_json is not None:
        report_dir = Path(args.write_report)
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "last-report.json").write_text(args.report_json)
    sys.stdout.write(args.stdout)
    sys.stderr.write(args.stderr)
    return args.exit


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
