#!/usr/bin/env python3
# meiva:note
# The publish step has three situations and only one of them may create anything: no tag (publish), the tag already on the certified commit (a re-dispatch of a failed certify — nothing to do), and the tag on any other commit (refuse). This is a module rather than a shell one-liner because the one-liner was untestable and wrong: `git rev-parse refs/tags/vX` answers with the TAG OBJECT's sha for an annotated tag — the only kind this gate creates — so the no-op branch could never be reached, and the refusal it fell through to named the tag as "not the certified commit" even when it was.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


# meiva:note
# `^{commit}` is the whole fix: it peels an annotated tag to the commit it marks, which is the only
# answer comparable with the commit the gate certified. A tag that is absent answers None; a tag
# that exists but is not a tag at all would also answer None, and by then the version is already
# fixed in the certificate, so refusing to publish is the safe reading of "nothing there".
def tagged_commit(repository: Path, version: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        (
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/v{version}^{{commit}}",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# meiva:note
# Pure decision, so both real-tag cases are proven against a repository (tests/test_tag.py) instead
# of asserted as strings in a workflow file.
def plan(tagged: str | None, target: str) -> str:
    if tagged is None:
        return "create"
    return "skip" if tagged == target else "refuse"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide what the publish step does with a tag.")
    parser.add_argument("--repo", default=".", type=Path, help="checkout to read git facts from")
    parser.add_argument("--version", required=True, help="version the certificate would publish")
    parser.add_argument("--target", required=True, help="commit the certificate certifies")
    arguments = parser.parse_args(argv)

    tagged = tagged_commit(arguments.repo, arguments.version)
    action = plan(tagged, arguments.target)
    if action == "refuse":
        print(
            f"::error::tag v{arguments.version} marks {tagged}, not the certified commit "
            f"{arguments.target}",
            file=sys.stderr,
        )
        return 1
    print(action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
