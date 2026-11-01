#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


# meiva:note
# The nightly verdict reads junit only: the duration leg retries a shard up to three times, so a job's exit code can be green after a first-attempt failure, and an auxiliary measurement step can fail while every test passed. Neither belongs in the answer to "was main green last night".
@dataclass(frozen=True)
class ShardResult:
    shard: str
    tests: int
    failures: int
    errors: int
    skipped: int

    @property
    def ok(self) -> bool:
        return self.failures == 0 and self.errors == 0 and self.tests > 0


@dataclass(frozen=True)
class NightlyVerdict:
    ok: bool
    reason: str
    shards: tuple[ShardResult, ...]


# meiva:note
# The junit read here is written by this repository's own CI run, never by an untrusted caller, so the stdlib parser is enough; a truncated file raises rather than passing.
def read_shard(path: Path) -> ShardResult:
    root = ElementTree.parse(path).getroot()  # noqa: S314
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError(f"{path}: no testsuite element")
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.get("tests", "0"))
        failures += int(suite.get("failures", "0"))
        errors += int(suite.get("errors", "0"))
        skipped += int(suite.get("skipped", "0"))
    return ShardResult(
        shard=path.stem,
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
    )


def judge(results: Sequence[ShardResult], expected: Mapping[str, int]) -> NightlyVerdict:
    by_prefix: dict[str, list[ShardResult]] = {}
    for result in results:
        suite = result.shard.split("-", 1)[0]
        by_prefix.setdefault(suite, []).append(result)

    missing: list[str] = []
    for suite, count in expected.items():
        if len(by_prefix.get(suite, [])) != count:
            missing.append(f"{suite}: expected {count} shards, got {len(by_prefix.get(suite, []))}")
    if missing:
        return NightlyVerdict(
            ok=False, reason="missing-shards: " + "; ".join(missing), shards=tuple(results)
        )

    red = [result.shard for result in results if not result.ok]
    if red:
        return NightlyVerdict(
            ok=False, reason="failed-shards: " + ", ".join(sorted(red)), shards=tuple(results)
        )
    return NightlyVerdict(ok=True, reason="all-shards-green", shards=tuple(results))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--junit-dir", type=Path, required=True, help="directory holding the junit files"
    )
    parser.add_argument("--sha", required=True, help="the commit the nightly run tested")
    parser.add_argument("--static-shards", type=int, default=8)
    parser.add_argument("--backend-shards", type=int, default=8)
    parser.add_argument("--e2e-shards", type=int, default=4)
    parser.add_argument(
        "--output", type=Path, required=True, help="path of the nightly record to write"
    )
    parser.add_argument("--run-url", default="", help="link to the run, recorded for the reader")
    arguments = parser.parse_args(argv)

    paths = sorted(arguments.junit_dir.rglob("*.xml"))
    results = [read_shard(path) for path in paths]
    verdict = judge(
        results,
        {
            "static": arguments.static_shards,
            "backend": arguments.backend_shards,
            "e2e": arguments.e2e_shards,
        },
    )
    record = {
        "sha": arguments.sha,
        "ok": verdict.ok,
        "reason": verdict.reason,
        "run": arguments.run_url,
        "shards": [
            {
                "shard": result.shard,
                "tests": result.tests,
                "failures": result.failures,
                "errors": result.errors,
                "skipped": result.skipped,
            }
            for result in verdict.shards
        ],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": verdict.ok, "reason": verdict.reason, "shards": len(verdict.shards)}))
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
