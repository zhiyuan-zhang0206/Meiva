# meiva:note
# Measure how far changes reach through the PR test-selection impact graph.
#
# Run: `python -m scripts.audit.impact_census [--top N] [--json]` from the repository
# root (stdlib-only, read-only, no environment). It reports the numbers the locality
# and selector workstreams track (issue #5128):
#
# - source files every collectable test reaches, overall and per top-level package
#   (locality drives this down; genuine foundation modules are its floor);
# - inputs the analysis cannot bound, how many every test reaches, and the tests that
#   reach any of them, which join every SELECTED subset (the selector side drives
#   these down);
# - the heaviest unbounded sites by the estimated time of the tests they pull in.
#
# Reading the checkout and its timing model is this shell's; the numbers themselves are
# `analysis/reach.py`.

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from scripts.audit.analysis import Census, census, report_lines
from scripts.ci.test_impact import build_impact
from scripts.ci.test_selector import estimate_seconds, load_checkout, load_durations

_REPO_ROOT = Path(__file__).resolve().parents[2]


def measure(repo_root: Path, *, top: int = 10) -> Census:
    # meiva:note
    # Build the impact graph and timing model of `repo_root`, then measure them.
    checkout = load_checkout(repo_root)
    tests = frozenset(checkout.collectable)
    impact = build_impact(checkout.repo_root, tests)
    durations = load_durations(checkout.repo_root / ".test_durations")
    return census(
        tests,
        impact.tests_by_input,
        tuple((item.path, item.line, item.reason) for item in impact.unknown),
        seconds_of=lambda selected: estimate_seconds(
            selected, durations, reference_paths=set(tests)
        ),
        top=top,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure how far changes reach through the PR test-selection impact graph."
    )
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--top", type=int, default=10, help="heaviest unbounded sites to list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = measure(args.repo_root.resolve(), top=args.top)
    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print("\n".join(report_lines(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
