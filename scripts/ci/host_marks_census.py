# meiva:note
# The runtime half of the host-lane gate (task #5531). The static gate reads the registry and
# the workflow and says a lane selects `-m requires_host`; this records what that selection
# actually collected on the host that ran it, and refuses a run whose selection is empty. The
# two halves answer different questions: the gate proves the lane exists, this proves it
# selected something. A lane that collected nothing goes green having tested nothing, and its
# junit report reads exactly like a lane that ran everything it was asked to.
#
# Run: `python -m scripts.ci.host_marks_census` (writes tmp/host-marks-census.json, exit 1 on
# an empty selection). The CI job uploads that file next to the junit report, so the census of
# one run can be diffed against another's.

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

# meiva:note
# The selection the registry promises for a `marked` lane, spelled the way pytest reads it.
SELECTION = ("-m", "requires_host")

# meiva:note
# Where the CI job's artifact step looks for the census; one constant so the workflow and the
# script cannot disagree about the file a reader opens.
DEFAULT_REPORT = "tmp/host-marks-census.json"

_REPO_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# `--collect-only -q` prints one node id per line and a summary after them. A node id is the
# only line whose file part ends in `.py` and whose first `::` names a test, which is what
# distinguishes it from the summary ("19/26139 tests collected (26120 deselected) in 15.38s")
# and from a warning that mentions a file.
def node_ids(output: str) -> list[str]:
    found: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if "::" not in stripped:
            continue
        file_part = stripped.split("::", 1)[0]
        if file_part.endswith(".py"):
            found.append(stripped)
    return found


# meiva:note
# The child is this interpreter and a fixed argv, with no shell and no interpolation, so the
# selection cannot pick up a word the environment supplies. A collection failure is reported as
# a collection failure rather than as an empty census: "the lane selected nothing" and "the lane
# could not select" are different defects and only one of them is about this gate.
def collect(root: Path) -> list[str]:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *SELECTION],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout[-4000:])
        sys.stderr.write(completed.stderr[-4000:])
        raise SystemExit(
            f"pytest --collect-only {(' '.join(SELECTION))} failed; an uncollectable lane is "
            "not an empty one, so this is reported as a collection failure"
        )
    return node_ids(completed.stdout)


# meiva:note
# The census file's shape, so the summary line and the JSON cannot disagree about it: a
# `dict[str, object]` here would type the node ids as `object` and force a cast at the one
# place that prints them.
class Census(TypedDict):
    host: str
    selection: str
    collected: int
    node_ids: list[str]


def report(ids: list[str], host: str) -> Census:
    return {
        "host": host,
        "selection": " ".join(SELECTION),
        "collected": len(ids),
        "node_ids": sorted(ids),
    }


def record(ids: list[str], path: Path, host: str) -> Census:
    if not ids:
        raise SystemExit(
            "the lane collected nothing: `-m requires_host` selected no test on this host, so "
            "a green run here would mean the lane tested nothing. Either the declarations are "
            "missing from this branch or the selection no longer matches them."
        )
    document = report(ids, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--report", type=Path, default=None)
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    path: Path = arguments.report or root / DEFAULT_REPORT
    ids = collect(root)
    document = record(ids, path, platform.system().lower())
    print(
        f"host-marks census: {document['collected']} declaration(s) selected on {document['host']}"
    )
    for node_id in document["node_ids"]:
        print(f"  {node_id}")
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
