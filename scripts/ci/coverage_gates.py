#!/usr/bin/env python3
"""Backend coverage gates — core-domain line rate + per-risk-domain floors.

Reads coverage.json (written by `uv run coverage json -o coverage.json` from
the combined shard data in the backend CI job) and enforces two tiers:

- the combined line-rate gate over the six core domains
  (agent/meiva/cli/gateway/base/ui) — the legacy 85% gate, unchanged;
- per-risk-domain minimum line floors for ops/services/meiva_builtins — the
  high-incident operational domains (deploy/backup/watchdog/ops) the
  combined gate never scored (tech audit 2026-08-24 finding #10).

Floors are set just below each domain's measured baseline at gate
introduction, so a regression below current coverage fails while
shard/flake variance passes. A floored domain with zero measured lines
fails regardless of its floor value: an empty domain (renamed package,
broken source glob) must be loud, never a vacuous pass. The measured
per-domain table is always printed (calibration + failure diagnosis);
exits 1 when any gate fails.

Usage: uv run python scripts/ci/coverage_gates.py [coverage.json]
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

# The 85% gate denominator — must match [tool.coverage.run].source's core
# set and the comment in ci.yml's gate step. ops/services/meiva_builtins are
# measured into the same data but gated by FLOORS, not by this number.
CORE_DOMAINS = ("agent", "meiva", "cli", "gateway", "base", "ui")
CORE_THRESHOLD_DEFAULT = 85.0

# Per-risk-domain minimum line floors (percent). Each key is a domain
# prefix: top-level packages ("ops") or second-level subdomains
# ("services/backup"). A prefix matches every reported file whose path is the
# prefix itself or starts with "prefix/".
#
# Calibrated 2026-08-29 from the first measuring CI run (PR #965):
# ops 92.6% / services 73.1% / meiva_builtins 81.6%. Floors sit 2-4 points
# below the measured baseline — enough buffer for shard-split and flake
# variance, tight enough that a real regression (a critical module losing
# its tests) trips the gate.
#
# ops is the deploy/rollout/cluster-lifecycle surface (highest incident
# exposure — a broken upgrade takes the fleet down), so it keeps the
# tightest buffer; services carries the data-durability daemons
# (backup/watchdog) and meiva_builtins the plugins.
FLOORS: dict[str, float] = {
    "ops": 90.0,
    "services": 70.0,
    "meiva_builtins": 78.0,
}


def _domain_of(path: str) -> str:
    return path.split("/", 1)[0]


def _subdomain_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _line_counts(path: str, entry: object) -> tuple[int, int]:
    if not isinstance(entry, dict):
        raise TypeError(f"{path}: coverage entry must be an object")
    summary = cast("dict[object, object]", entry).get("summary")
    if not isinstance(summary, dict):
        raise TypeError(f"{path}: coverage summary must be an object")
    counts = cast("dict[object, object]", summary)
    covered, valid = counts.get("covered_lines"), counts.get("num_statements")
    if type(covered) is not int or type(valid) is not int:
        raise TypeError(f"{path}: coverage line counts must be integers")
    if not 0 <= covered <= valid:
        raise ValueError(f"{path}: coverage line counts must satisfy 0 <= covered <= total")
    return covered, valid


def _aggregate(
    files: Mapping[str, object],
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Aggregate covered/valid line counts from the coverage.json file map.

    Returns (per_domain, per_subdomain): per_domain keys are top-level
    packages ("ops"); per_subdomain keys are the first two path segments
    ("services/backup") — both maps hold [covered, valid] pairs.
    """
    per_domain: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_subdomain: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for path, entry in files.items():
        covered, valid = _line_counts(path, entry)
        per_domain[_domain_of(path)][0] += covered
        per_domain[_domain_of(path)][1] += valid
        per_subdomain[_subdomain_of(path)][0] += covered
        per_subdomain[_subdomain_of(path)][1] += valid
    return per_domain, per_subdomain


def _rate(covered: int, valid: int) -> float:
    return round(covered * 100.0 / valid, 1) if valid else 0.0


def _prefix_rate(
    per_domain: dict[str, list[int]],
    per_subdomain: dict[str, list[int]],
    prefix: str,
) -> tuple[float, int, int]:
    """Aggregated (rate, covered, valid) for a domain prefix."""
    covered = valid = 0
    table = per_subdomain if "/" in prefix else per_domain
    for name, (c, v) in table.items():
        if _matches_prefix(name, prefix):
            covered += c
            valid += v
    return _rate(covered, valid), covered, valid


def _core_rate(per_domain: dict[str, list[int]]) -> tuple[float, int, int]:
    covered = sum(per_domain[d][0] for d in CORE_DOMAINS)
    valid = sum(per_domain[d][1] for d in CORE_DOMAINS)
    return _rate(covered, valid), covered, valid


def _print_domain_table(per_domain: dict[str, list[int]], floors: Mapping[str, float]) -> None:
    print("all measured domains (covered/total lines):")
    for name, (covered, valid) in sorted(per_domain.items(), key=lambda kv: kv[1][1], reverse=True):
        note = ""
        if name in CORE_DOMAINS:
            note = " — core gate"
        elif name in floors:
            rate = _rate(covered, valid)
            verdict = "ok" if rate >= floors[name] else f"FAIL (floor {floors[name]}%)"
            note = f" — floor {floors[name]}% {verdict}"
        print(f"  {name:>20} {_rate(covered, valid):>6}% ({covered}/{valid}){note}")


def _print_floored_subdomains(
    per_domain: dict[str, list[int]],
    per_subdomain: dict[str, list[int]],
    floors: Mapping[str, float],
) -> None:
    floored_subdomains = [p for p in floors if "/" in p]
    if not floored_subdomains:
        return
    print("floored subdomains:")
    for prefix in floored_subdomains:
        rate, covered, valid = _prefix_rate(per_domain, per_subdomain, prefix)
        verdict = "ok" if rate >= floors[prefix] else f"FAIL (floor {floors[prefix]}%)"
        print(f"  {prefix:>20} {rate:>6}% ({covered}/{valid}) — floor {floors[prefix]}% {verdict}")


def check(files: Mapping[str, object], threshold: float, *, floors: Mapping[str, float]) -> int:
    """Enforce the gates against a coverage.json `files` map.

    Returns 0 when every gate passes, 1 otherwise. Always prints the
    per-domain table.
    """
    per_domain, per_subdomain = _aggregate(files)
    failures: list[str] = []

    core_rate, core_covered, core_valid = _core_rate(per_domain)
    print(
        f"core domains {'+'.join(CORE_DOMAINS)}: {core_rate}% "
        f"({core_covered}/{core_valid} lines) — threshold {threshold}%"
    )
    if core_rate < threshold:
        failures.append(f"core combined coverage {core_rate}% below {threshold}%")

    _print_domain_table(per_domain, floors)
    _print_floored_subdomains(per_domain, per_subdomain, floors)

    for prefix, floor in floors.items():
        rate, _covered, valid = _prefix_rate(per_domain, per_subdomain, prefix)
        if valid == 0:
            # Existence check, independent of the floor value: a floored
            # domain with no measured lines means the package vanished or
            # the source glob broke — fail even at floor 0.0, never a
            # vacuous pass.
            failures.append(
                f"{prefix} has no measured lines — package missing or source glob broken"
            )
            continue
        if rate < floor:
            failures.append(f"{prefix} coverage {rate}% below floor {floor}%")

    if failures:
        print("coverage gates FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("coverage gates passed")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    floors: Mapping[str, float],
    env: Mapping[str, str],
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    json_path = args[0] if args else "coverage.json"
    threshold = float(env.get("BACKEND_COVERAGE_THRESHOLD", CORE_THRESHOLD_DEFAULT))
    with Path(json_path).open() as f:
        data: object = json.load(f)
    if not isinstance(data, dict):
        raise TypeError("coverage report must be an object")
    files = cast("dict[str, object]", data).get("files")
    if not isinstance(files, dict):
        raise TypeError("coverage report files must be an object")
    return check(cast("dict[str, object]", files), threshold, floors=floors)


if __name__ == "__main__":
    sys.exit(main(floors=FLOORS, env=os.environ))
