#!/usr/bin/env python3
# meiva:note
# The certificate is the only artifact that manufactures release eligibility, and nothing compared its version with the product version the tree reports: gate run 40822741370 would have stamped v1.0.1 onto a tree whose pyproject.toml says 1.0.0, so tag, record and artifacts would disagree from the first release onward.
# meiva:note
# The label must match the tree; that does not make the product version a gate input. It drives no test selection and no compatibility verdict, and the two axes stay independent — only the name is required to be the name the tree carries.
# meiva:note
# The version is read from the TARGET COMMIT, never from the checkout: the gate accepts a target_sha that may name another commit, and a working-tree read would pass exactly the pairs this refuses.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

# meiva:note
# The same three-part shape base.py parses (`_RELEASE_TAG`), leading zeros included: a second,
# stricter rule here would refuse a label that base.py and the record round-trip without complaint.
_RELEASE_LABEL = re.compile(r"^\d+\.\d+\.\d+$")


# meiva:note
# The target commit's own pyproject.toml, read through git rather than the filesystem: a caller may certify any commit, and the checkout is usually a different one.
def product_version_at(repository: Path, revision: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        ("git", "-C", str(repository), "show", f"{revision}:pyproject.toml"),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    # meiva:note
    # Stdlib parsing, like base.py: the precheck must cost seconds and cannot fail on an environment problem. Importing `base.host...` is not an option here either — this directory holds `base.py`, which shadows the package of the same name.
    try:
        version = tomllib.loads(result.stdout)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) else None


# meiva:note
# Pure decision, so the refusals can be tested without a repository. `bad-label` and `no-tree-version` are named separately from `version-mismatch`: they call for a different fix, and a single "refused" would hide which one happened.
def judge(label: str, tree_version: str | None) -> tuple[bool, str]:
    if _RELEASE_LABEL.match(label) is None:
        return False, "bad-label"
    if tree_version is None:
        return False, "no-tree-version"
    if label != tree_version:
        return False, "version-mismatch"
    return True, "matches-tree"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refuse a certificate label the target tree does not carry."
    )
    parser.add_argument("--repo", default=".", type=Path, help="checkout to read git facts from")
    parser.add_argument("--target", default="HEAD", help="commit the certificate would name")
    parser.add_argument(
        "--version", required=True, dest="label", help="version the gate is asked to certify"
    )
    arguments = parser.parse_args(argv)

    tree_version = product_version_at(arguments.repo, arguments.target)
    ok, reason = judge(arguments.label, tree_version)
    print(
        json.dumps(
            {
                "ok": ok,
                "reason": reason,
                "label": arguments.label,
                "tree_version": tree_version,
                "target": arguments.target,
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
