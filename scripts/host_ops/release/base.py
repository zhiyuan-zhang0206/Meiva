#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROVENANCE_DIRECTORY = "release"
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_REQUIRED_KEYS = ("version", "sha", "gate_run", "passed")


# meiva:note
# Eligibility reads release/<version>.json, never a tag: a hand-made vX.Y.Z cannot manufacture release eligibility, and only a strict three-part version parses as a release so the historical milestone tags (v0.2-eval-driven, ...) stay out.
# meiva:link docs/conventions/engineering/host-versioning.md
@dataclass(frozen=True)
class ReleaseRecord:
    version: str
    sha: str
    gate_run: str
    passed: bool
    hotfix: bool = False
    followup_gate_run: str | None = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    base: ReleaseRecord | None = None


def parse_release_tag(tag: str) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.match(tag)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def version_key(version: str) -> tuple[int, int, int] | None:
    return parse_release_tag(f"v{version}")


def load_records(directory: Path) -> list[ReleaseRecord]:
    records: list[ReleaseRecord] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in _REQUIRED_KEYS if key not in payload]
        if missing:
            raise ValueError(f"{path}: missing {', '.join(missing)}")
        records.append(
            ReleaseRecord(
                version=str(payload["version"]),
                sha=str(payload["sha"]),
                gate_run=str(payload["gate_run"]),
                passed=bool(payload["passed"]),
                hotfix=bool(payload.get("hotfix", False)),
                followup_gate_run=payload.get("followup_gate_run"),
            )
        )
    return records


# meiva:note
# A hotfix that reached production before its gate ran is not a usable base until a follow-up gate run is recorded on it; is_ancestor answers git's merge-base --is-ancestor semantic and arrives as a callable so the decision runs with no repository.
def verify_release_base(
    target_sha: str,
    records: Iterable[ReleaseRecord],
    is_ancestor: Callable[[str, str], bool],
) -> Verdict:
    records = list(records)
    if not records:
        return Verdict(ok=False, reason="no-provenance")

    usable: list[ReleaseRecord] = []
    unfollowed_hotfix = False
    for record in records:
        if not record.passed:
            continue
        if record.hotfix and not record.followup_gate_run:
            if is_ancestor(record.sha, target_sha):
                unfollowed_hotfix = True
            continue
        if is_ancestor(record.sha, target_sha):
            usable.append(record)

    if usable:
        base = max(usable, key=lambda record: version_key(record.version) or (0, 0, 0))
        return Verdict(ok=True, reason=f"descends-from-release/{base.version}", base=base)
    if unfollowed_hotfix:
        return Verdict(ok=False, reason="hotfix-not-followed-up")
    return Verdict(ok=False, reason="no-gated-ancestor")


# meiva:note
# The only effect in this module: fixed git queries (no shell), reading the caller-chosen checkout.
def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        check=False,
    )


def _resolved_sha(repository: Path, revision: str) -> str | None:
    result = _git(repository, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="HEAD", help="revision the wave would deploy")
    parser.add_argument("--repo", default=".", type=Path, help="checkout to read git facts from")
    parser.add_argument(
        "--provenance-dir",
        default=PROVENANCE_DIRECTORY,
        type=Path,
        help="directory below the repository holding release/<version>.json",
    )
    arguments = parser.parse_args(argv)

    target_sha = _resolved_sha(arguments.repo, arguments.target)
    if target_sha is None:
        print(json.dumps({"ok": False, "reason": "unknown-target", "target": arguments.target}))
        return 1
    records = load_records(arguments.repo / arguments.provenance_dir)
    verdict = verify_release_base(
        target_sha,
        records,
        lambda base, commit: (
            _git(arguments.repo, "merge-base", "--is-ancestor", base, commit).returncode == 0
        ),
    )
    print(
        json.dumps(
            {
                "ok": verdict.ok,
                "reason": verdict.reason,
                "target": target_sha,
                "base_version": verdict.base.version if verdict.base else None,
                "base_sha": verdict.base.sha if verdict.base else None,
            }
        )
    )
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
