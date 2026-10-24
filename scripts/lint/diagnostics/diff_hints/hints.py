# meiva:note
# The shell of the diff hints: it reads git, builds the values the engine takes, and
# prints what the renderer returns. Three commands.
#
#   hints.py commit-msg MSGFILE     the hook of the commit-msg stage: the staged diff must be
#                                   answered by the `Hint-Ack` trailers of this message.
#   hints.py branch                 the hook of the pre-push stage: the diff of merge-base
#                                   (origin/main, HEAD)..HEAD must be answered by the
#                                   trailers of any commit on the branch.
#   hints.py report BASE HEAD       every site of a diff, answered or not, for calibration.
#
# The rule, the catalog and the trailer format are in
# scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md.
# meiva:link scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# meiva:note
# A standalone script: the repository root goes on the path before the first import of
# the repository's own packages.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT))

from meizel.roles import all_packages, packages  # noqa: E402
from scripts.lint.diagnostics.diff_hints.acks import parse_acks, unanswered, unused  # noqa: E402
from scripts.lint.diagnostics.diff_hints.diff import parse_diff  # noqa: E402
from scripts.lint.diagnostics.diff_hints.engine import analyze  # noqa: E402
from scripts.lint.diagnostics.diff_hints.model import (  # noqa: E402
    Ack,
    FileChange,
    FileKind,
    Roles,
    Site,
)
from scripts.lint.diagnostics.diff_hints.render import render_errors, render_open  # noqa: E402
from scripts.lint.diagnostics.diff_hints.scope import classify  # noqa: E402

_DIFF = ("diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--diff-filter=ACMRTD")


class GitError(RuntimeError):
    pass


# meiva:note
# Fixed argument vectors only: git from PATH, never a shell, never user text as a command.
def _git(*args: str) -> bytes:
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "core.quotePath=false", *args],
        capture_output=True,
        check=False,
    )
    if done.returncode:
        message = done.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {message}")
    return done.stdout


def _roles() -> Roles:
    return Roles(
        production=frozenset(packages("application", "templates")),
        tests=frozenset(packages("tests")),
        first_party=frozenset(all_packages()),
    )


# meiva:note
# The text of each path at `rev` (the index when `rev` is empty), by one `git cat-file
# --batch`. A path git cannot find is left out of the result.
def _read_blobs(rev: str, paths: Sequence[str]) -> dict[str, str]:
    if not paths:
        return {}
    request = "".join(f"{rev}:{path}\n" for path in paths).encode()
    done = subprocess.run(
        ["git", "cat-file", "--batch"], input=request, capture_output=True, check=False
    )
    if done.returncode:
        raise GitError(f"git cat-file --batch failed: {done.stderr.decode('utf-8', 'replace')}")
    out, at = done.stdout, 0
    texts: dict[str, str] = {}
    for path in paths:
        end = out.index(b"\n", at)
        header = out[at:end].split()
        at = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts[path] = out[at : at + size].decode("utf-8", "replace")
            at += size + 1
    return texts


def _diff(
    spec: Sequence[str], rev: str, roles: Roles
) -> tuple[tuple[FileChange, ...], dict[str, str]]:
    patch = _git(*_DIFF, *spec, "--", "*.py").decode("utf-8", "replace")
    changes = parse_diff(patch)
    live = [
        c.path for c in changes if not c.deleted and classify(c.path, roles) is not FileKind.OTHER
    ]
    return changes, _read_blobs(rev, live)


def _sites(spec: Sequence[str], rev: str) -> list[Site]:
    roles = _roles()
    changes, texts = _diff(spec, rev, roles)
    return analyze(changes, texts, roles)


def _base(head: str) -> str:
    try:
        _git("rev-parse", "--verify", "-q", "origin/main")
    except GitError as error:
        raise GitError("pre-push requires a local origin/main; fetch origin main first") from error
    return _git("merge-base", "origin/main", head).decode().strip()


def _verdict(
    sites: list[Site], acks: tuple[Ack, ...], errors: tuple[str, ...], *, branch: bool
) -> int:
    open_sites = unanswered(sites, acks, ignore_line=branch)
    for ack in [] if branch else unused(acks, sites):
        message = f"note: the answer `{ack.hint.value} {ack.path}` matches no open site"
        sys.stderr.write(f"diff hints: {message}\n")
    if errors:
        sys.stderr.write(render_errors(errors) + "\n")
    if open_sites:
        sys.stderr.write(render_open(open_sites) + "\n")
    return 1 if (open_sites or errors) else 0


def _commit_msg(message_file: str) -> int:
    message = Path(message_file).read_text(encoding="utf-8", errors="replace")
    acks, errors = parse_acks(message)
    return _verdict(_sites(["--cached"], ""), acks, errors, branch=False)


def _branch(head: str) -> int:
    base = _base(head)
    raw = _git("log", "--format=%B%x00", f"{base}..{head}").decode("utf-8", "replace")
    parsed = [parse_acks(message) for message in raw.split("\0")]
    acks = tuple(ack for found, _ in parsed for ack in found)
    errors = tuple(error for _, found in parsed for error in found)
    return _verdict(_sites([base, head], head), acks, errors, branch=True)


def _report(base: str, head: str, *, as_json: bool) -> int:
    sites = _sites([base, head], head)
    for site in sites:
        if as_json:
            row = {
                "hint": site.hint.value,
                "path": site.path,
                "line": site.line,
                "detail": site.detail,
            }
            sys.stdout.write(json.dumps(row) + "\n")
        else:
            sys.stdout.write(f"{site.path}:{site.line}: hint {site.hint.value}: {site.detail}\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diff hints: questions a commit message answers.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("commit-msg").add_argument("message_file")
    commands.add_parser("branch").add_argument("--head", default="HEAD")
    report = commands.add_parser("report")
    report.add_argument("base")
    report.add_argument("head")
    report.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "commit-msg":
            return _commit_msg(args.message_file)
        if args.command == "branch":
            return _branch(args.head)
        return _report(args.base, args.head, as_json=args.json)
    except GitError as error:
        sys.stderr.write(f"diff hints: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
