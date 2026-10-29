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
#
# A batch's tree also carries the batches queued before it, because the queue merges each one onto
# the tree it tested last. Those changes are not in this batch's record, and their removals were
# declared where they were made, so a removal this batch's own pull requests do not answer is
# followed to the commit that took the declaration away and read from that commit's own pull request.
# Nothing else can answer it: the record of a change is the record of the pull request that made it,
# never one that happens to name the same entry. A removal whose commit cannot be found, or whose
# pull request cannot be read, stays unanswered with the reason named - the one direction this file
# never reports as clean.
# An added entry is reported and never asked about: a widening is what the reviewer reads in the
# diff, not a declaration to write.
# meiva:link meizel/docs/checker/policy/policy.meiva.okf.md


from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

# meiva:note
# A standalone script: the repository root goes on the path before the first import of the
# repository's own packages.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from meizel.allowances import (  # noqa: E402
    POLICY,
    Declaration,
    Policy,
    RemovalReport,
    load,
    parse_snapshot,
    removal_report,
)
from meizel.checkout import GitRepo, toplevel  # noqa: E402
from meizel.graph.model import MeizelError  # noqa: E402
from scripts.ci.test_impact import member_numbers  # noqa: E402

_SEPARATOR = " \u2014 "

# meiva:note
# `gh` answers in seconds; a read that hangs is a record this run cannot read, never a step that
# waits out the job's own timeout.
_GH_TIMEOUT_S = 60
# meiva:note
# What a run says when a removal stands and the record that would answer it could not be read. It
# fails: an unreadable record is missing evidence, and a declaration this run never held cannot be
# reported as one that was not needed. A batch with nothing removed never reaches this line.
_NOT_JUDGED = (
    "a Trunk batch whose pull request(s) could not be read in this run, while a declaration stands "
    "removed: no record here could answer it, so this run fails rather than report an unanswered "
    "removal as cleared"
)

# meiva:note
# Reading one pull request's title and body, by number: the single thing a test replaces, so the
# queue path is exercised without a network or a token.
MemberReader = Callable[[int], str | None]

# meiva:note
# The pull request a commit belongs to, by its SHA: the second thing a test replaces, so the layers a
# batch inherits are exercised without a network or a token.
CommitReader = Callable[[str], tuple[int, ...]]


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
    # Whether the record was read from the pull requests a Trunk batch tests. Such a tree carries
    # the batches queued before it, so this flag is what says a removal may belong to another change.
    inherited: bool = False


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
# A batch's record: the title and body of every pull request its body names, read through `gh`. When
# not one of them can be read the record is empty and does not judge — see `Record`.
def _batch(request: dict[str, object], read_member: MemberReader) -> Record:
    numbers = member_numbers(str(request.get("body") or ""))
    texts = [read_member(number) for number in numbers]
    named = ", ".join(str(number) for number in numbers) or "none named"
    source = f"the pull request(s) this batch tests ({named})"
    readable = [text for text in texts if text is not None]
    if not readable:
        return Record("", source, judged=False, inherited=True)
    return Record("\n\n".join(readable), source, inherited=True)


# meiva:note
# Every pull request a commit belongs to, through the API the job's token reads. All of them, not the
# first: a commit can be reachable from more than one pull request, and a declaration in the one that
# removed the entry on an earlier occasion would otherwise answer for this removal. An empty answer
# is a record this run does not have, never a record with nothing to declare.
def _gh_pull_requests_of(sha: str) -> tuple[int, ...]:
    try:
        done = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls", "--jq", ".[].number"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if done.returncode != 0:
        return ()
    return tuple(int(line) for line in done.stdout.split() if line.isdigit())


# meiva:note
# The policy at a commit, and why it is not readable: a commit this checkout does not hold, a file
# the commit does not carry (legitimate - a change may introduce the file), or text that does not
# parse. A missing object is never read as an empty policy, which would report "nothing removed".
def _policy_at(repo: GitRepo, ref: str) -> tuple[Policy | None, str]:
    try:
        repo.run_text("cat-file", "-e", ref)
    except MeizelError:
        return None, f"commit {ref} is not in this checkout"
    try:
        repo.run_text("cat-file", "-e", f"{ref}:{POLICY}")
    except MeizelError:
        return Policy(), ""
    text = repo.show_optional_text(ref, POLICY)
    if text is None:
        return None, f"{ref}:{POLICY} is there but cannot be read"
    policy, problems = parse_snapshot(text)
    if problems:
        return None, f"{ref}:{POLICY}: {problems[0]}"
    return policy, ""


# meiva:note
# The history the attribution reads, fetched when a batch has a removal to attribute and never
# before: a batch's tree carries the batches queued before it, and naming the commit that made one of
# their removals needs that commit's parents. The whole history is the one fetch that assumes nothing
# about dates or depth; a checkout that stays shallow is reported, not guessed at.
def _complete_history(repo: GitRepo) -> tuple[str, ...]:
    try:
        if repo.run_text("rev-parse", "--is-shallow-repository").strip() != "true":
            return ()
        repo.run_text("fetch", "--unshallow", "origin", "HEAD")
    except MeizelError as error:
        return (f"this checkout is shallow and its history could not be completed: {error}",)
    return ()


# meiva:note
# The commits a shallow checkout stops at inside `base..HEAD`: a commit whose parents are not here
# cannot be shown to have removed anything, and its absence is not "nothing removed it". This is the
# one place that decides whether the range is complete, so the depth of the checkout is never assumed.
def _truncated(repo: GitRepo, base: str) -> tuple[str, ...]:
    path = repo.run_text("rev-parse", "--git-path", "shallow").strip()
    try:
        grafted = set(Path(repo.root, path).read_text(encoding="utf-8").split())
    except OSError:
        return ()
    inside = grafted & set(repo.run_text("rev-list", f"{base}..HEAD").split())
    return tuple(
        f"this checkout is shallow: it stops at commit {sha[:12]}, inside {base[:12]}..HEAD"
        for sha in sorted(inside)[:1]
    )


# meiva:note
# The declarations one version of the file makes, keyed the way a `Policy-Removal` line names them.
def _declared(policy: Policy | None) -> set[str]:
    return (
        set()
        if policy is None
        else {item.named for item in removal_report(policy, Policy(), "").removed}
    )


# meiva:note
# The removals a merge made itself, as `declaration -> merge`: entries that both parents still carry
# and the merge does not. A merge that only carries a branch's change teaches nothing here - that
# change has its own commit, which `_owners` reads - but a conflict resolved by deleting an entry is
# a removal no commit made, and it is reported as one nobody can declare rather than dropped.
def _resolutions(repo: GitRepo, base: str) -> dict[str, str]:
    found: dict[str, str] = {}
    listing = repo.run_text(
        "log", "--merges", "--format=%H", "--reverse", f"{base}..HEAD", "--", POLICY
    )
    for merge in listing.split():
        parents = repo.run_text("rev-list", "--parents", "-n", "1", merge).split()[1:]
        if len(parents) < 2:
            continue
        sides = [_policy_at(repo, parent)[0] for parent in parents[:2]]
        if any(side is None for side in sides):
            continue
        carried = _declared(sides[0]) & _declared(sides[1])
        for name in carried - _declared(_policy_at(repo, merge)[0]):
            found[name] = merge
    return found


# meiva:note
# The commit that took each declaration away: the non-merge commits `base..HEAD` that touched the
# file, each parsed before and after, so a commit is named for exactly the entries its own diff
# removed - never for an entry that shares a word with it. The last commit that removed an entry
# wins, since a later re-addition is not a removal.
def _owners(repo: GitRepo, base: str) -> tuple[dict[str, str], tuple[str, ...]]:
    found: dict[str, str] = {}
    problems: list[str] = []
    listing = repo.run_text(
        "log", "--no-merges", "--format=%H", "--reverse", f"{base}..HEAD", "--", POLICY
    )
    for commit in listing.split():
        before, why_before = _policy_at(repo, f"{commit}^")
        after, why_after = _policy_at(repo, commit)
        if before is None or after is None:
            problems.append(
                f"commit {commit[:12]} changed {POLICY} and one of its two versions cannot be read "
                f"({why_before or why_after}), so what it removed is not attributable"
            )
            continue
        for item in removal_report(before, after, "").removed:
            found[item.named] = commit
    return found, tuple(problems)


# meiva:note
# What a batch inherited, and what it can say about it. A Trunk test tree carries the batches queued
# before it, so a declaration this batch's own members do not answer may still have been declared by
# the change that removed it - and only by that change: the record read here is the one belonging to
# the commit that took the entry away, never a pull request that happens to name it. `answered`
# carries every pull request the commit belongs to, because a commit reachable from two of them is
# answered by both declaring the line, or by neither.
@dataclass(frozen=True)
class Attribution:
    answered: tuple[tuple[Declaration, tuple[int, ...], str], ...] = ()
    problems: tuple[str, ...] = ()


# meiva:note
# Why a removal has no commit to follow: a conflict resolved into it, a checkout that stops before
# the change, or a range that genuinely holds no such change. Three different answers, because they
# ask for three different repairs.
def _unowned(name: str, base: str, resolutions: dict[str, str], stopped: tuple[str, ...]) -> str:
    if name in resolutions:
        return (
            f"merge {resolutions[name][:12]} removed {name} while resolving a merge: a merge carries "
            "no pull request, so no record can declare it"
        )
    if stopped:
        return f"{stopped[0]}, so which change removed {name} is not readable"
    return f"no commit in {base}..HEAD took {name} away"


# meiva:note
# The pull requests that answer a removal, or the reason none does. Its own batch's record was
# already read and did not answer, so a commit that record covers decides nothing here; every other
# pull request the commit belongs to has to declare the line, and one that cannot be read is a
# missing record rather than a quiet answer.
def _declared_by(
    item: Declaration,
    commit: str,
    before: Policy,
    after: Policy,
    mine: set[int],
    bodies: dict[int, str | None],
    read_commit: CommitReader,
    read_member: MemberReader,
) -> tuple[tuple[int, ...] | None, str | None]:
    numbers = read_commit(commit)
    if not numbers:
        return None, f"commit {commit[:12]} took {item.named} away and no pull request names it"
    if mine.intersection(numbers):
        return None, None
    unread: list[int] = []
    declared: list[bool] = []
    for number in numbers:
        if number not in bodies:
            bodies[number] = read_member(number)
        text = bodies[number]
        if text is None:
            unread.append(number)
            continue
        declared.append(item not in removal_report(before, after, text).unanswered)
    if unread:
        return None, (
            f"pull request(s) {', '.join(str(number) for number in unread)} took {item.named} away "
            f"in commit {commit[:12]} and their bodies could not be read"
        )
    if all(declared):
        return tuple(sorted(numbers)), None
    if len(numbers) > 1:
        return None, (
            f"commit {commit[:12]} is in pull requests "
            f"{', '.join(str(number) for number in sorted(numbers))}: not every one declares "
            f"{item.named}, so this removal is ambiguous"
        )
    return None, None


# meiva:note
# Every unanswered removal of a batch, followed to the change that made it: a removal whose own pull
# request declares it is answered, and one whose owner this run cannot read stays unanswered with the
# reason named. Nothing here can answer a declaration its own change did not remove, a merge that
# resolved a conflict into a removal is named as one no record can declare, and a commit several
# pull requests contain needs all of them to declare it.
def _attribution(
    repo: GitRepo,
    base: str,
    before: Policy,
    after: Policy,
    report: RemovalReport,
    request: dict[str, object],
    read_member: MemberReader,
    read_commit: CommitReader,
) -> Attribution:
    history = _complete_history(repo)
    owners, unreadable = _owners(repo, base)
    resolutions = _resolutions(repo, base)
    stopped = _truncated(repo, base)
    mine = {_number(request), *member_numbers(str(request.get("body") or ""))}
    bodies: dict[int, str | None] = {}
    answered: list[tuple[Declaration, tuple[int, ...], str]] = []
    named: list[str] = []
    left = False
    for item in report.unanswered:
        commit = owners.get(item.named)
        if commit is None:
            named.append(_unowned(item.named, base, resolutions, stopped))
            left = True
            continue
        settled, problem = _declared_by(
            item, commit, before, after, mine, bodies, read_commit, read_member
        )
        if problem is not None:
            named.append(problem)
        elif settled is not None:
            answered.append((item, settled, commit))
    # meiva:note
    # A commit whose own copy cannot be read says nothing about the removals that were traced, so it
    # is reported only when one was left without an owner - there it explains the gap.
    return Attribution(tuple(answered), tuple(named) + (unreadable + history if left else ()))


# meiva:note
# The number of the pull request the event names, or 0 when it names none: the batch reads its own
# members' bodies and must not read them a second time as if they came from another change.
def _number(request: dict[str, object]) -> int:
    number = request.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else 0


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


def main(
    argv: list[str] | None = None,
    *,
    read_member: MemberReader = _gh_text,
    read_commit: CommitReader = _gh_pull_requests_of,
) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args, read_member, read_commit)
    except (MeizelError, ValueError) as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, read_member: MemberReader, read_commit: CommitReader) -> int:
    repo = GitRepo(toplevel(Path(args.repo)))
    before, after = _policies(repo, args.against)
    request = _pull_request(args.event)
    record = _record(repo, args.against, args.record, request, read_member)
    report = removal_report(before, after, record.text)
    # meiva:note
    # Only a batch carries changes it did not make, and only a record this run could read can be
    # asked about them: an unreadable batch is reported and passes, as it always was.
    attribution = (
        _attribution(repo, args.against, before, after, report, request, read_member, read_commit)
        if record.inherited and record.judged and report.unanswered
        else Attribution()
    )
    report = _answered_elsewhere(report, attribution)
    print(_log(args.against, record, report, attribution))
    if not _unjudged(record, report):
        _complain(record.source, report)
    if args.record_out and record.judged:
        Path(args.record_out).write_text(record.text, encoding="utf-8")
    summary = _summary(args.against, record, report, args.on_removal, attribution)
    if args.summary:
        _append(Path(args.summary), summary)
    print(summary)
    return _exit_code(record, report, args.on_removal)


# meiva:note
# The file at the ref through the snapshot reader, which is the one historical decoder, and the
# working tree through the live reader, so a policy that no longer parses stops the gate.
def _policies(repo: GitRepo, against: str) -> tuple[Policy, Policy]:
    before, why = _policy_at(repo, against)
    if before is None:
        raise MeizelError(f"the comparison base {against} cannot be read: {why}")
    return before, load(repo.root)


# meiva:note
# The text the declarations are read from, in the one order this gate promises. A named file wins
# because a caller asked for it; otherwise a pull request's own title and body are the record,
# because their text is the squash message that lands; a Trunk batch replaces that body with the
# queue's own, so there the record is the pull request(s) the batch tests; and a run with no pull
# request reads the commits, which is what a local run has.
def _record(
    repo: GitRepo,
    against: str,
    record: str | None,
    request: dict[str, object],
    read_member: MemberReader,
) -> Record:
    if record:
        return Record(Path(record).read_text(encoding="utf-8"), f"the file {record}")
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


# meiva:note
# The report with every removal the change that made it declares taken out of `unanswered`, and the
# reasons an owner could not be read added to the problems this run fails on. A removal this run
# cannot trace stays unanswered: nothing about a missing owner is read as green.
def _answered_elsewhere(report: RemovalReport, attribution: Attribution) -> RemovalReport:
    settled = {item.named for item, _, _ in attribution.answered}
    left = tuple(item for item in report.unanswered if item.named not in settled)
    return replace(report, unanswered=left, problems=report.problems + attribution.problems)


def _log(
    against: str, record: Record, report: RemovalReport, attribution: Attribution | None = None
) -> str:
    attribution = attribution or Attribution()
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
    for item, numbers, commit in attribution.answered:
        named = ", ".join(str(number) for number in numbers)
        lines.append(
            f"meizel: [{item.table}] {item.shown} was taken away by pull request {named} "
            f"(commit {commit[:12]}), which declares it"
        )
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
# about. With nothing removed there is nothing to confirm, so a batch that changed nothing stays
# quiet and pays no read of its own; with one removed, a record this run never held is missing
# evidence, and the run fails naming it.
def _unjudged(record: Record, report: RemovalReport) -> bool:
    return not record.judged and bool(report.removed)


def _exit_code(record: Record, report: RemovalReport, on_removal: str) -> int:
    if _unjudged(record, report):
        return 1
    if report.problems:
        return 1
    if not report.unanswered:
        return 0
    return 1 if on_removal == "fail" else 0


# meiva:note
# The markdown the reviewer sees: the counts first, because a removal and an addition must not read
# the same at a glance, then the unanswered removals with the exact lines that would answer them.
def _summary(
    against: str,
    record: Record,
    report: RemovalReport,
    on_removal: str,
    attribution: Attribution | None = None,
) -> str:
    attribution = attribution or Attribution()
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
    if attribution.answered:
        body.extend(
            [
                f"**{len(attribution.answered)} removal(s) answered by the change that made them:** "
                "a Trunk batch tests the batches queued before it too, and each removal is answered "
                "by the pull request that took the declaration away:",
                "",
            ]
        )
        body.extend(
            f"- `[{item.table}] {item.shown}` - pull request "
            f"{', '.join(str(number) for number in numbers)}, commit {commit[:12]}"
            for item, numbers, commit in attribution.answered
        )
        body.append("")
    if attribution.problems:
        body.extend(["**A removal this run cannot attribute, so it is not answered:**", ""])
        body.extend(f"- {problem}" for problem in attribution.problems)
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
