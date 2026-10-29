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
# The record is the pull request's title and body, whose text is the squash message that lands. A
# Trunk batch is the exception: the queue writes that body itself, and its branch keeps each member's
# own commit behind a queue merge that this job's shallow checkout never walks to, so there the record
# is the pull request(s) the batch names and tests, read through `gh`; a run that cannot read them
# lists the removals and passes. A run with no pull request reads the commits `--against..HEAD`, which
# is what a local run has. `--record` names a file to read instead.
# An added entry is reported and never asked about: a widening is what the reviewer reads in the
# diff, not a declaration to write.
# meiva:link meizel/docs/checker/policy.meiva.okf.md


from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
from meizel.graph.model import MeizelError  # noqa: E402
from meizel.graph.universe import GitRepo, toplevel  # noqa: E402

_SEPARATOR = " \u2014 "

# meiva:note
# `gh` answers in seconds; a read that hangs is a record this run cannot read, never a step that
# waits out the job's own timeout.
_GH_TIMEOUT_S = 60
_NOT_JUDGED = (
    "a Trunk batch whose own body carries no declaration, and whose pull request(s) could not be "
    "read in this run: the removals above are left to review and the run passes"
)
_MEMBER_SECTION = re.compile(r"## Pull Requests Being Tested(.*?)(?=\n## |\Z)", re.DOTALL)
_MEMBER_LINK = re.compile(r"\[(\d+)\]\([^)\s]*/pull/\1\)")

# meiva:note
# Reading one pull request's title and body, by number: the single thing a test replaces, so the
# queue path is exercised without a network or a token.
MemberReader = Callable[[int], str | None]


# meiva:note
# The text the declarations are read from, where it came from — both named in the log and the check
# summary — and whether this run can judge at all. A Trunk batch is the one record that may not: its
# body is the queue's, and the pull request(s) it names live behind an API this run may not reach.
# Reporting and passing is the right answer then, because a false red costs the whole batch its place
# in the merge queue (pull request 7603 was ejected that way three times, about 36 minutes of CI),
# while an unanswered removal is still in the diff for its reviewer.
@dataclass(frozen=True)
class Record:
    text: str
    source: str
    judged: bool = True


# meiva:note
# One pull request's title and body through the `gh` CLI, which reads the token the job was given.
# Every failure — no `gh`, no token, an answer that is not a pull request — is the same answer, None:
# a record this run cannot read, which the gate reports instead of guessing at.
def _gh_text(number: int) -> str | None:
    try:
        done = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", "title,body"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        payload: object = json.loads(done.stdout)
    except json.JSONDecodeError:
        return None
    found = _object(payload)
    return f"{found.get('title') or ''!s}\n\n{found.get('body') or ''!s}"


# meiva:note
# The pull request numbers a batch's body names, out of the section the queue writes for them: that
# body also links the base commit and the queue page, and a member is a link whose text is its own
# number, so no other prose in it can pass for one.
def member_numbers(body: str) -> list[int]:
    section = _MEMBER_SECTION.search(body)
    if section is None:
        return []
    return [int(match.group(1)) for match in _MEMBER_LINK.finditer(section.group(1))]


# meiva:note
# A batch's record: the title and body of every pull request its body names, read through `gh`. When
# not one of them can be read the record is empty and does not judge — see `Record`.
def _batch(request: dict[str, object], read_member: MemberReader) -> Record:
    numbers = member_numbers(str(request.get("body") or ""))
    texts = [read_member(number) for number in numbers]
    named = ", ".join(str(number) for number in numbers) or "none named"
    source = f"the pull request(s) this batch tests ({named})"
    readable = [text for text in texts if text is not None]
    if not readable:
        return Record("", source, judged=False)
    return Record("\n\n".join(readable), source)


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
    parser.add_argument(
        "--record-out",
        metavar="path",
        help=(
            "write the record itself to this file, so the steps after this one read the same text: "
            "`debt diff --symbol-moves-file` takes its `Symbol-Move` lines from here"
        ),
    )
    return parser


def main(argv: list[str] | None = None, *, read_member: MemberReader = _gh_text) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args, read_member)
    except (MeizelError, ValueError) as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, read_member: MemberReader) -> int:
    repo = GitRepo(toplevel(Path(args.repo)))
    before, after = _policies(repo, args.against)
    record = _record(repo, args.against, args.record, args.event, read_member)
    report = removal_report(before, after, record.text)
    print(_log(args.against, record, report))
    if not _unjudged(record, report):
        _complain(record.source, report)
    if args.record_out and record.judged:
        Path(args.record_out).write_text(record.text, encoding="utf-8")
    summary = _summary(args.against, record, report, args.on_removal)
    if args.summary:
        _append(Path(args.summary), summary)
    print(summary)
    return _exit_code(record, report, args.on_removal)


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
# because their text is the squash message that lands; a Trunk batch replaces that body with the
# queue's own, so there the record is the pull request(s) the batch tests; and a run with no pull
# request reads the commits, which is what a local run has.
def _record(
    repo: GitRepo, against: str, record: str | None, event: str | None, read_member: MemberReader
) -> Record:
    if record:
        return Record(Path(record).read_text(encoding="utf-8"), f"the file {record}")
    request = _pull_request(event)
    if request and not _queued(request):
        title = str(request.get("title") or "")
        body = str(request.get("body") or "")
        return Record(f"{title}\n\n{body}", "the pull request title and body")
    if request:
        return _batch(request, read_member)
    commits = repo.run_text("log", "--format=%B", f"{against}..HEAD")
    return Record(commits, f"the commits {against}..HEAD")


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


def _log(against: str, record: Record, report: RemovalReport) -> str:
    lines = [
        f"{POLICY} against {against}: {len(report.removed)} removed, {len(report.added)} added "
        f"(record: {record.source})"
    ]
    lines.extend(f"- [{item.table}] {item.shown}" for item in report.removed)
    lines.extend(f"+ [{item.table}] {item.shown}" for item in report.added)
    if _unjudged(record, report):
        lines.append(f"meizel: not judged: {_NOT_JUDGED}")
        return "\n".join(lines)
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


# meiva:note
# A run that cannot judge: a record it could not read and a removal that would have been asked
# about. With nothing removed there is nothing to judge, so a batch that changed nothing stays quiet.
def _unjudged(record: Record, report: RemovalReport) -> bool:
    return not record.judged and bool(report.removed)


def _exit_code(record: Record, report: RemovalReport, on_removal: str) -> int:
    if _unjudged(record, report):
        return 0
    if report.problems:
        return 1
    if not report.unanswered:
        return 0
    return 1 if on_removal == "fail" else 0


# meiva:note
# The markdown the reviewer sees: the counts first, because a removal and an addition must not read
# the same at a glance, then the unanswered removals with the exact lines that would answer them.
def _summary(against: str, record: Record, report: RemovalReport, on_removal: str) -> str:
    body = [
        f"### Allowance policy: {len(report.removed)} removed, {len(report.added)} added",
        "",
        f"Against `{against}`; record: {record.source}. A removal needs a `Policy-Removal` line in the "
        f"pull request body; an addition needs nothing. This run: `--on-removal={on_removal}`.",
        "",
    ]
    body.extend(_table(report))
    if _unjudged(record, report):
        body.extend([f"**Not judged here:** {_NOT_JUDGED}.", ""])
    elif report.unanswered:
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
