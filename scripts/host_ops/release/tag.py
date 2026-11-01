#!/usr/bin/env python3
# meiva:note
# The publish step has three situations and only one of them may create anything: no tag (create),
# the tag already on the certified commit (a re-dispatch of a partial certify — skip), and the tag
# on any other commit (refuse). `^{commit}` peels an annotated tag to the commit it marks rather
# than the tag object SHA.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:link scripts/docs/scripts.meiva.okf.md
from cli.fleet_update.plan.release_target import parse_release_tag  # noqa: E402
from scripts.host_ops.release.git_tags import resolved_commit  # noqa: E402

TagAction = Literal["create", "skip", "refuse"]


def tagged_commit(repository: Path, version: str) -> str | None:
    if parse_release_tag(version) is None:
        raise ValueError(
            f"version {version!r} must be a canonical CalVer tag (vYYYY.MM.DD.HHMMSS[-hfN])"
        )
    return resolved_commit(repository, f"refs/tags/{version}")


def plan(tagged: str | None, target: str) -> TagAction:
    if tagged is None:
        return "create"
    return "skip" if tagged == target else "refuse"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide what the publish step does with a tag.")
    parser.add_argument("--repo", default=".", type=Path, help="checkout to read git facts from")
    parser.add_argument(
        "--version",
        required=True,
        help="canonical CalVer tag (vYYYY.MM.DD.HHMMSS[-hfN]) the certificate publishes",
    )
    parser.add_argument("--target", required=True, help="commit the certificate certifies")
    arguments = parser.parse_args(argv)

    try:
        tagged = tagged_commit(arguments.repo, arguments.version)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    decision = plan(tagged, arguments.target)
    if decision == "refuse":
        print(
            f"::error::tag {arguments.version} marks {tagged}, not the certified commit "
            f"{arguments.target}",
            file=sys.stderr,
        )
        return 1
    print(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
