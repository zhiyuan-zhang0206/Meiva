#!/usr/bin/env python3
# meiva:note
# Verify whether a candidate revision descends from a certified `meiva-release/v1` annotated Git
# tag. Non-CalVer tags (`v0.1.0`, `m2-full`, `channel/*`) are ignored, while any CalVer-named tag
# (`vYYYY.MM.DD.HHMMSS[-hfN]`) with a missing or tampered certificate fails loudly.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:link scripts/docs/scripts.meiva.okf.md
from cli.fleet_update.plan.release_target import (  # noqa: E402
    ReleaseCertificate,
    ReleaseVersion,
    parse_release_tag,
)
from scripts.host_ops.release.git_tags import (  # noqa: E402
    is_ancestor,
    list_tag_names,
    read_verified_certificate,
    resolved_commit,
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    base: ReleaseCertificate | None = None


def version_key(version: str) -> ReleaseVersion | None:
    return parse_release_tag(version)


def load_certified_releases(repository: Path) -> list[ReleaseCertificate]:
    certified: list[tuple[ReleaseVersion, ReleaseCertificate]] = []
    for tag_name in list_tag_names(repository):
        parsed = parse_release_tag(tag_name)
        if parsed is None:
            continue
        cert, reason = read_verified_certificate(repository, tag_name)
        if cert is None:
            raise ValueError(reason)
        certified.append((parsed, cert))
    certified.sort(key=lambda pair: pair[0])
    return [cert for _ver, cert in certified]


def has_release_history(repository: Path) -> bool:
    return any(parse_release_tag(name) is not None for name in list_tag_names(repository))


def verify_release_base(
    target_sha: str,
    certificates: Iterable[ReleaseCertificate],
    ancestor_predicate: Callable[[str, str], bool],
    *,
    bootstrap_allowed: bool = False,
) -> Verdict:
    items = list(certificates)
    if not items:
        if bootstrap_allowed:
            return Verdict(ok=True, reason="bootstrap-no-release-history")
        return Verdict(ok=False, reason="no-provenance")

    usable: list[tuple[ReleaseVersion, ReleaseCertificate]] = []
    for cert in items:
        parsed = parse_release_tag(cert.version)
        if parsed is None:
            raise ValueError(f"certificate version {cert.version!r} is not a CalVer release tag")
        if ancestor_predicate(cert.sha, target_sha):
            usable.append((parsed, cert))

    if usable:
        _ver, best = max(usable, key=lambda pair: pair[0])
        return Verdict(ok=True, reason=f"descends-from-release/{best.version}", base=best)
    return Verdict(ok=False, reason="no-gated-ancestor")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a revision descends from a certified release tag."
    )
    parser.add_argument("--target", default="HEAD", help="revision to verify")
    parser.add_argument("--repo", default=".", type=Path, help="checkout to read git facts from")
    parser.add_argument(
        "--bootstrap-when-empty",
        action="store_true",
        help="allow certifying the first release when no CalVer release tag exists yet",
    )
    arguments = parser.parse_args(argv)

    target_sha = resolved_commit(arguments.repo, arguments.target)
    if target_sha is None:
        print(json.dumps({"ok": False, "reason": "unknown-target", "target": arguments.target}))
        return 1
    try:
        certs = load_certified_releases(arguments.repo)
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "invalid-certificate",
                    "detail": str(exc),
                    "target": target_sha,
                }
            )
        )
        return 1
    bootstrap_allowed = arguments.bootstrap_when_empty and not has_release_history(arguments.repo)
    verdict = verify_release_base(
        target_sha,
        certs,
        lambda ancestor, commit: is_ancestor(arguments.repo, ancestor, commit),
        bootstrap_allowed=bootstrap_allowed,
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
