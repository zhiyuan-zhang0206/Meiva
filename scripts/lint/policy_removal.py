#!/usr/bin/env python3
# meiva:note
# The gate: a declaration removed from `meizel.policy.toml` must say why in the pull request.
# `meizel.policy.toml` is the one closed list of allowances, so an entry that disappears narrows what
# the checker reports while both comparisons against the base stay green: `check` finds nothing left
# to report, and `debt diff` sees debt disappear, the one direction it allows. This shell reads that
# narrowing out of `meizel.allowances` and asks for one line per removal:
#
#   Policy-Removal: <table>[.<entry>] — <why it is no longer needed> — <what replaces it, or the gap it leaves>
#
# The record is the pull request's title and body, whose text is the squash message that lands. A run
# without one reads the commits `--against..HEAD`: a local run, and the Trunk merge branch, which
# carries each pull request's landing message as a commit. `--record` names a file to read instead.
# An added entry is reported and never asked about: a widening is what the reviewer reads in the
# diff, not a declaration to write.
# meiva:link meizel/docs/checker/policy.meiva.okf.md


from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# meiva:note
# A standalone script: the repository root goes on the path before the first import of the
# repository's own packages.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from meizel.allowances import (  # noqa: E402
    POLICY,
    Policy,
    RemovalReport,
    load,
    parse_snapshot,
    removal_report,
)
from meizel.checkout import GitRepo, toplevel  # noqa: E402
from meizel.graph.model import MeizelError  # noqa: E402

_SEPARATOR = " \u2014 "


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="policy_removal.py",
        description="Fail when a declaration left meizel.policy.toml unanswered.",
    )
    parser.add_argument("--repo", default=".", help="repository root (default: the cwd)")
    parser.add_argument("--against", required=True, metavar="ref", help="the ref to compare with")
    parser.add_argument(
        "--on-removal",
        required=True,
        choices=["fail", "warn"],
        help="what an unanswered removal does: fail the run, or only report it",
    )
    parser.add_argument(
        "--record",
        metavar="path",
        help="read the text from this file instead of the pull request or the commits",
    )
    parser.add_argument(
        "--event",
        metavar="path",
        help="a GitHub event payload: its pull request title and body are the record",
    )
    parser.add_argument(
        "--summary",
        metavar="path",
        help="append the markdown report to this file (the CI job summary)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except (MeizelError, ValueError) as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    repo = GitRepo(toplevel(Path(args.repo)))
    before, after = _policies(repo, args.against)
    text, source = _record(repo, args.against, args.record, args.event)
    report = removal_report(before, after, text)
    print(_log(args.against, source, report))
    _complain(source, report)
    summary = _summary(args.against, source, report, args.on_removal)
    if args.summary:
        _append(Path(args.summary), summary)
    print(summary)
    return _exit_code(report, args.on_removal)


# meiva:note
# The file at the ref through the snapshot reader, which is the one historical decoder, and the
# working tree through the live reader, so a policy that no longer parses stops the gate.
def _policies(repo: GitRepo, against: str) -> tuple[Policy, Policy]:
    text = repo.show_optional_text(against, POLICY)
    if text is None:
        return Policy(), load(repo.root)
    before, problems = parse_snapshot(text)
    if problems:
        raise MeizelError(f"{against}:{POLICY}: {problems[0]} (the ref has no readable policy)")
    return before, load(repo.root)


# meiva:note
# The text the declarations are read from, in the one order this gate promises. A named file wins
# because a caller asked for it; otherwise a pull request's own title and body are the record,
# because their text is the squash message that lands; the Trunk merge branch replaces that body
# with the queue's, so there the commits are the record, and they are what a local run has anyway.
def _record(repo: GitRepo, against: str, record: str | None, event: str | None) -> tuple[str, str]:
    if record:
        return Path(record).read_text(encoding="utf-8"), f"the file {record}"
    request = _pull_request(event)
    if request and not _queued(request):
        title = str(request.get("title") or "")
        body = str(request.get("body") or "")
        return f"{title}\n\n{body}", "the pull request title and body"
    return repo.run_text("log", "--format=%B", f"{against}..HEAD"), (f"the commits {against}..HEAD")


# meiva:note
# The event's pull request as a string-keyed mapping; empty when the event has none.
def _pull_request(event: str | None) -> dict[str, object]:
    if not event:
        return {}
    try:
        payload: object = json.loads(Path(event).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _object(_object(payload).get("pull_request"))


# meiva:note
# A JSON object as a string-keyed mapping: the payload is a document this tool did not build, so
# every step down it is checked once here and the values below stay `object`.
def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _queued(request: dict[str, object]) -> bool:
    ref = _object(request.get("head")).get("ref")
    return isinstance(ref, str) and ref.startswith("trunk-")


def _log(against: str, source: str, report: RemovalReport) -> str:
    lines = [
        f"{POLICY} against {against}: {len(report.removed)} removed, {len(report.added)} added "
        f"(record: {source})"
    ]
    lines.extend(f"- [{item.table}] {item.shown}" for item in report.removed)
    lines.extend(f"+ [{item.table}] {item.shown}" for item in report.added)
    if report.declared:
        lines.append(f"meizel: {len(report.declared)} `Policy-Removal` line(s) read")
    for line in report.unused:
        lines.append(
            f"meizel: warning: `{line.declared}` answers no removed declaration; "
            "delete the line or name the entry it meant"
        )
    if not report.removed:
        lines.append("meizel: no declaration was removed")
    elif not report.unanswered:
        lines.append("meizel: every removed declaration is answered")
    return "\n".join(lines)


def _complain(source: str, report: RemovalReport) -> None:
    for problem in report.problems:
        print(f"meizel: {problem}", file=sys.stderr)
    if not report.unanswered:
        return
    print(
        f"meizel: {len(report.unanswered)} removed declaration(s) have no `Policy-Removal` line "
        f"in {source}:",
        file=sys.stderr,
    )
    for item in report.unanswered:
        print(f"  [{item.table}] {item.shown}", file=sys.stderr)
    print(
        "Add one line per removal to the pull request body (its text is the squash message; "
        "a local run reads the commits instead):",
        file=sys.stderr,
    )
    for line in report.templates():
        print(f"  {line}", file=sys.stderr)


def _exit_code(report: RemovalReport, on_removal: str) -> int:
    if report.problems:
        return 1
    if not report.unanswered:
        return 0
    return 1 if on_removal == "fail" else 0


# meiva:note
# The markdown the reviewer sees: the counts first, because a removal and an addition must not read
# the same at a glance, then the unanswered removals with the exact lines that would answer them.
def _summary(against: str, source: str, report: RemovalReport, on_removal: str) -> str:
    body = [
        f"### Allowance policy: {len(report.removed)} removed, {len(report.added)} added",
        "",
        f"Against `{against}`; record: {source}. A removal needs a `Policy-Removal` line in the "
        f"pull request body; an addition needs nothing. This run: `--on-removal={on_removal}`.",
        "",
    ]
    body.extend(_table(report))
    if report.unanswered:
        verdict = "unanswered" if on_removal == "fail" else "unanswered (reported only)"
        body.extend([f"**{len(report.unanswered)} removed declaration(s) {verdict}:**", ""])
        body.extend(f"- `[{item.table}] {item.shown}`" for item in report.unanswered)
        body.extend(["", "One `Policy-Removal` line answers each of them:", ""])
        body.extend(f"- `{line}`" for line in report.templates())
        body.append("")
    for line in report.unused:
        body.append(f"- note: `{line.declared}` answers no removed declaration.")
    return "\n".join(body).rstrip() + "\n"


def _table(report: RemovalReport) -> list[str]:
    rows: dict[str, list[int]] = {}
    for side, items in enumerate((report.removed, report.added)):
        for item in items:
            rows.setdefault(item.table, [0, 0])[side] += 1
    if not rows:
        return []
    lines = ["| table | removed | added |", "| --- | ---: | ---: |"]
    lines.extend(
        f"| `{table}` | {counts[0]} | {counts[1]} |" for table, counts in sorted(rows.items())
    )
    return [*lines, ""]


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


if __name__ == "__main__":
    raise SystemExit(main())
