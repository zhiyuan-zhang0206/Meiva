# meiva:note
# The merge gate's shell: it resolves which checkout's `merge_gate.py` judges, which grant list
# applies, and then hands over to that module. It holds no merge condition of its own -- those live
# in `merge_gate.py`, and changing the rules there must never require editing this file.
#
# Why it lives in the repository (2026-11-08, task #6411): the module moved here on 2026-11-07 but
# this shell stayed in one operator's workspace, so the half every lane actually runs had no test
# and no CI. A gate whose shell is untested is a gate whose failure modes -- calling the module
# twice, swallowing its exit code, printing something the module did not -- stay invisible until one
# of them lets a wrong merge through.
# meiva:note
# Module resolution, in order:
#   1. $MEIVA_GATE_MODULE   an explicit path; how the tests point at a stub
#   2. ~/.meiva/source/scripts/ci/pull_requests/gate/merge_gate.py   the deployed checkout (at main)
#   3. ~/Meiva/scripts/ci/pull_requests/gate/merge_gate.py           the development checkout
# Deployed before development on purpose: a gate that judges merges should be the code that is
# actually running, not what somebody happens to have checked out. The chosen module is printed on
# stderr with its checkout's short HEAD sha, so the answer and its age are observed rather than
# assumed -- after a rollout the deployed tree wins, and a deployed tree behind main would judge new
# pull requests by old rules behind an identical-looking path.
# meiva:note
# Grant: $MEIVA_LANE_FILE, else ~/.meiva/gates/lane-agents.txt. That file names which agents may
# merge, so it stays outside the repository; a missing list fails closed here rather than letting the
# module decide what an empty grant means.
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

CANDIDATES = (
    Path.home() / ".meiva/source/scripts/ci/pull_requests/gate/merge_gate.py",
    Path.home() / "Meiva/scripts/ci/pull_requests/gate/merge_gate.py",
)
DEFAULT_LANE_FILE = Path.home() / ".meiva/gates/lane-agents.txt"


def gate_module() -> Path:
    override = os.environ.get("MEIVA_GATE_MODULE")
    if override:
        return Path(override).expanduser().resolve()
    for candidate in CANDIDATES:
        if candidate.is_file():
            return candidate
    sys.exit(
        "merge_ready: the gate module is in none of "
        + ", ".join(str(candidate) for candidate in CANDIDATES)
        + "; set MEIVA_GATE_MODULE to the merge_gate.py of a checkout"
    )


# meiva:note
# A missing grant list is a refusal, not an empty list: "nobody may merge" and "this host has no
# authority table" look identical to the module, and only the second one is a configuration error
# someone has to fix.
def lane_file() -> Path:
    override = os.environ.get("MEIVA_LANE_FILE")
    path = Path(override).expanduser() if override else DEFAULT_LANE_FILE
    if not path.is_file():
        sys.exit(f"merge_ready: no lane grant list at {path}; create it or set MEIVA_LANE_FILE")
    return path


# meiva:note
# The gate is written in the repository's runtime (3.12). This file stays plain enough to run on an
# older interpreter, so the mismatch is reported here instead of surfacing as a SyntaxError from
# inside the module -- on macOS /usr/bin/python3 is 3.9, and a PATH without the Meiva venv picks it up.
def require_runtime() -> None:
    # meiva:note
    # The version check is the reason this file exists separately from the module it forwards: ruff
    # reads the repository's floor (3.12) and calls the guard outdated, so the directive below is the
    # exception that keeps the one runtime fact a caller on macOS needs to see.
    # The tuple is copied so the comparison stays a run-time fact: pyright reads the repository's
    # floor, decides the literal guard is always false, and reports the exit below unreachable.
    version = tuple(sys.version_info[:2])
    if version < (3, 12):
        sys.exit(
            f"merge_ready: this is Python {sys.version.split()[0]}; the gate needs 3.12. "
            "Put the Meiva checkout's .venv first on PATH, or run this file with "
            "<checkout>/.venv/bin/python3."
        )


# meiva:note
# The checkout is the nearest ancestor holding `.git`, not a fixed number of parents: the gate sits
# at `scripts/ci/pull_requests/gate/` today and the tests point the shell at a stub elsewhere, so a
# depth written into this file would be wrong for one of them the moment either moves.
def checkout_of(module: Path) -> Path:
    for candidate in module.parents:
        if (candidate / ".git").exists():
            return candidate
    sys.exit(f"merge_ready: no git checkout above {module}")


def describe(checkout: Path) -> str:
    # meiva:note
    # Two reads of the checkout, both by absolute name and both read-only: the short HEAD sha, and
    # whether the gate's own directory is dirty. Their directives are this note's.
    revision = subprocess.run(  # noqa: S603
        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = revision.stdout.strip() if revision.returncode == 0 else "unknown"
    status = subprocess.run(  # noqa: S603
        ["git", "-C", str(checkout), "status", "--porcelain", "--", "scripts/ci/pull_requests"],
        capture_output=True,
        text=True,
        check=False,
    )
    return f"{sha}+dirty" if status.stdout.strip() else sha


def main(argv: list[str]) -> NoReturn:
    require_runtime()
    module = gate_module()
    grant = lane_file()
    checkout = checkout_of(module)
    print(
        f"merge_ready: gate={module}@{describe(checkout)} grant={grant}",
        file=sys.stderr,
    )
    # meiva:note
    # The gate is run from that checkout's root, because `gh api repos/{owner}/{repo}` resolves the
    # placeholder from the current repository -- running from the caller's directory failed with
    # "fatal: not a git repository".
    os.chdir(checkout)
    # meiva:note
    # Replacement, not capture: the gate becomes this process, so "runs once, prints exactly what it
    # printed, exits with its code" holds by construction rather than by a wrapper reproducing three
    # things correctly. Each of the three a child-process wrapper can get wrong looks like a verdict.
    os.execv(sys.executable, [sys.executable, str(module), *argv])  # noqa: S606


if __name__ == "__main__":
    main(sys.argv[1:])
