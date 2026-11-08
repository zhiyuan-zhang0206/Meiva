# meiva:note
# The merge gate for the pyright/debt lanes: five conditions, one fail-closed verdict.
#
# `python3 scripts/ci/pull_requests/gate/merge_gate.py <PR number|url>`, from a checkout of the
# repository whose pull requests it judges -- the `repos/{owner}/{repo}` call resolves from the
# current directory, so a wrapper has to start inside a checkout. It prints the evidence per
# condition and one verdict line; exit 0 = GO, 1 = NO-GO, and an unreadable check is NO-GO.
#
#   1 head    the PR body pins `Preflight on head <headRefOid>:` and carries `PREFLIGHT PASS`
#   2 checks  every check run is green (Trunk queue rows ignored; no check run at all = NO-GO)
#   3 files   the files the PR touches and the files main moved since the PR's base are disjoint,
#             except the owed-pyright list and the generated pyproject region (`SHARED_ALLOWED`);
#             a compare truncated at GitHub's 300-file cap cannot be trusted
#   4 text    the body carries a where_used line and a behaviour section
#   5 lane    the commit trailers name an agent `lane-agents.txt` covers. The ordinary pyright
#             pull requests are the user's own Claude Code line and are not this lane's to merge,
#             however green they look.
#
# `--selftest` runs condition 5's cases. `MEIVA_MERGE_REVIEWER=1` merges outside the lanes and
# waives 1, 4 and 5.
#
# Environment: `MEIVA_REAL_GH` (absolute path to the real `gh`; default: `gh` on PATH),
# `MEIVA_LANE_FILE` (the grant, default `lane-agents.txt` beside this module), `MEIVA_LANE_AGENTS`
# (comma-separated ids, overriding the file), `MEIVA_HANDED_OVER_PREFIX` (the branch prefix whose
# ladder may carry a harness trailer alone), `MEIVA_MERGE_REVIEWER`. The process reads them once,
# in the `__main__` block, and `main` takes them as arguments.

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import NamedTuple, TypeGuard

FAIL = "NO-GO"
OK = "GO"

# meiva:note
# The conclusions that do not block a merge. `skipped` and `neutral` are GitHub's own "this run
# carried no verdict"; a run that is still queued or in progress is neither, so it blocks.
GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

# meiva:note
# The one overlap condition 3 accepts. Every "make one prefix strict" pull request touches these
# two files by construction -- the owed list (one line) and the generated pyproject region (3-22
# lines) -- so requiring an empty intersection would serialise ~90 pull requests behind a
# 17-minute CI rerun each. 7381 measured the real risk instead: at prefix distance >= 2 a
# three-way merge is clean and `gen --check` passes on the merged tree, at distance 1 it
# conflicts, and `merge=union` on two adjacent deletions silently keeps both lines in the wrong
# order with no conflict marker. So the merge is safe to ATTEMPT, and the caller must run
# `gen_pyright_test_environments.py --check` on main afterwards: only that observes the result.
SHARED_ALLOWED = frozenset({"scripts/codegen/pyright_tests_owing_any.txt", "pyproject.toml"})

# meiva:note
# GitHub's own cap on the files a compare returns. At the cap the response is truncated with no
# marker, so a smaller intersection is not evidence of a smaller one.
COMPARE_FILE_LIMIT = 300

DEFAULT_LANE_FILE = Path(__file__).resolve().with_name("lane-agents.txt")


# meiva:note
# A gate input that could not be read: the gate reports it and refuses.
class MergeGateError(Exception):
    pass


# meiva:note
# `json.loads` is typed as returning `Any`. Passing the function itself keeps the call outside
# this module and hands back `object`, so the decoded answer is narrowed by the guards below
# instead of being bound to an unchecked name. `base/host/boundary/untyped.py` owns the same
# helper for in-tree callers; this copy exists because the gate runs outside a checkout and
# imports nothing first-party.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_dict(value) and all(isinstance(key, str) for key in value)


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def json_object(value: object, what: str) -> dict[str, object]:
    if not is_object_dict(value):
        raise MergeGateError(f"{what}: expected a JSON object, got {type(value).__name__}")
    return value


def json_rows(value: object, what: str) -> list[object]:
    if not is_object_list(value):
        raise MergeGateError(f"{what}: expected a JSON array, got {type(value).__name__}")
    return value


def text_field(source: Mapping[str, object], key: str, what: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        raise MergeGateError(f"{what}.{key}: expected a string, got {type(value).__name__}")
    return value


def optional_text(source: Mapping[str, object], key: str) -> str:
    value = source.get(key)
    return value if isinstance(value, str) else ""


class GhApi:
    # meiva:note
    # `gh api` over one resolved executable, so no function below reads the world.

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def json(self, path: str) -> object:
        result = subprocess.run(
            [self.executable, "api", "-H", "Accept: application/vnd.github+json", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise MergeGateError(
                f"gh api {path} failed rc={result.returncode}: {result.stderr.strip()[:400]}"
            )
        return object_of(json.loads, result.stdout)


# meiva:note
# The real `gh`, resolved from the environment the caller hands over rather than from this
# process's own: `MEIVA_REAL_GH` when the `gh` wrapper that invoked the gate sets it, else the
# first `gh` on the caller's PATH.
def real_gh(env: Mapping[str, str]) -> str:
    found = env.get("MEIVA_REAL_GH") or shutil.which("gh", path=env.get("PATH"))
    if not found:
        raise MergeGateError("real gh not found (set MEIVA_REAL_GH)")
    return found


def pr_number(argv: list[str]) -> int:
    for argument in argv:
        found = re.search(r"/pull/(\d+)", argument) or re.fullmatch(r"(\d+)", argument)
        if found:
            return int(found.group(1))
    raise MergeGateError("no PR number in arguments")


def lane_verdict(
    trailers: set[str], harness: set[str], lanes: set[str], *, handed_over: bool, reviewer: bool
) -> str | None:
    # meiva:note
    # Condition 5 as a pure function, so `--selftest` can pin every branch.
    #
    #     Returns None when the pull request belongs to a lane the grant covers, else the reason it
    #     does not. The trailer decides authorship; the handed-over ladder is the only case that may
    #     ship a harness trailer without a Meiva one.
    unknown = trailers - lanes
    if reviewer:
        return None
    if trailers and not unknown:
        return None
    if handed_over:
        return (
            f"trailer agent(s) {sorted(unknown)} outside the grant {sorted(lanes)}"
            if unknown
            else None
        )
    if harness:
        return (
            f"harness trailer present ({sorted(harness)[0]}) and no Meiva trailer "
            f"from a granted worker {sorted(lanes)}"
        )
    if not trailers:
        return "no `Co-authored-by: Meiva #<id>` trailer - not a lane PR"
    return f"trailer agent(s) {sorted(unknown)} outside the grant {sorted(lanes)}"


def selftest() -> int:
    lanes = {"7381", "3230"}
    cases: list[tuple[set[str], set[str], bool, bool, bool, str]] = [
        # meiva:note
        # (trailers, harness, handed_over, reviewer, expect_ok, label)
        ({"7381"}, set(), False, False, True, "plain worker PR"),
        (
            {"7381"},
            {"Co-Authored-By: Claude Sonnet 5.5"},
            False,
            False,
            True,
            "worker PR that also copied a harness tail",
        ),
        (set(), {"Co-Authored-By: Claude Sonnet 5.5"}, True, False, True, "handed-over ladder"),
        (
            set(),
            {"Co-Authored-By: Claude Sonnet 5.5"},
            False,
            False,
            False,
            "harness tail outside the ladder",
        ),
        (set(), set(), False, False, False, "no trailer at all"),
        ({"9999"}, set(), False, False, False, "trailer outside the grant"),
        ({"9999"}, set(), True, False, False, "outside-grant trailer on the ladder"),
        (set(), set(), False, True, True, "reviewer mode"),
    ]
    bad = 0
    for trailers, harness, handed, reviewer, expect_ok, label in cases:
        reason = lane_verdict(trailers, harness, lanes, handed_over=handed, reviewer=reviewer)
        ok = reason is None
        mark = "ok  " if ok == expect_ok else "FAIL"
        if ok != expect_ok:
            bad += 1
        print(f"{mark} {label}: {'allowed' if ok else reason}")
    print(f"\n{len(cases) - bad}/{len(cases)} cases as expected")
    return 1 if bad else 0


class CheckRun(NamedTuple):
    # meiva:note
    # One check run on the head commit.

    name: str
    status: str
    conclusion: str | None
    started_at: str


def check_runs(rows: Iterable[object]) -> list[CheckRun]:
    runs: list[CheckRun] = []
    for index, raw in enumerate(rows):
        row = json_object(raw, f"check_runs[{index}]")
        conclusion = row.get("conclusion")
        runs.append(
            CheckRun(
                name=text_field(row, "name", "check run"),
                status=text_field(row, "status", "check run"),
                conclusion=conclusion if isinstance(conclusion, str) else None,
                started_at=optional_text(row, "started_at"),
            )
        )
    return runs


# meiva:note
# One name can have several runs on the same head (a rerun, or the same check in two jobs). Keep
# the LATEST by `started_at` per name: an older cancelled duplicate must not block a head that a
# newer run already passed, because the latest run is the only one describing that check now.
def latest_by_name(runs: Iterable[CheckRun], *, skip_named: str = "trunk") -> dict[str, CheckRun]:
    latest: dict[str, CheckRun] = {}
    for run in runs:
        if skip_named in run.name.lower():
            continue
        previous = latest.get(run.name)
        if previous is None or run.started_at >= previous.started_at:
            latest[run.name] = run
    return latest


def red_runs(runs: Iterable[CheckRun]) -> list[CheckRun]:
    return [
        run for run in runs if run.status != "completed" or run.conclusion not in GREEN_CONCLUSIONS
    ]


def intersection_verdict(shared: Iterable[str], *, truncated: bool) -> str | None:
    # meiva:note
    # Condition 3: the overlap the merge may carry, or the reason it may not.
    overlap = sorted(shared)
    if truncated:
        return (
            f"compare truncated at {COMPARE_FILE_LIMIT} files - intersection cannot be trusted; "
            "rebase and re-run"
        )
    extra = sorted(set(overlap) - SHARED_ALLOWED)
    if extra:
        return (
            f"{len(overlap)} file(s) touched by both sides, including {extra[:3]} outside the "
            f"generated-region pair - rebase on current main and re-run preflight"
        )
    return None


# meiva:note
# Conditions 1 and 4 read the pull-request body, which is where this repository records both the
# preflight result and authorship: a squash merge keeps the body and drops the head commit
# message. See `trailer_sets` for the authorship half.
def preflight_verdict(body: str, head_sha: str, *, reviewer: bool) -> str | None:
    if reviewer:
        return None
    if (
        f"Preflight on head {head_sha}:" in body
        and "PREFLIGHT PASS: /trunk merge is allowed" in body
    ):
        return None
    stale = [line for line in body.splitlines() if line.startswith("Preflight on head")]
    if not stale:
        return "preflight PASS is not pinned to the current head"
    pinned = stale[-1][len("Preflight on head ") :][:12]
    return f"preflight PASS is not pinned to the current head (body pins {pinned}...)"


# meiva:note
# The heading may be written in any of the repository's three spellings; the third is spelled with
# escapes because source is English-only and functional CJK is written this way
# (runtime-identical).
def body_text_findings(body: str) -> tuple[bool, bool]:
    lowered = body.lower()
    has_where = "where_used" in lowered or "where used" in lowered
    heading = re.search(r"^#+\s*(behavior|behaviour|\u884c\u4e3a)", body, re.M | re.I)
    return has_where, heading is not None


def body_text_reasons(body: str, *, reviewer: bool) -> list[str]:
    if reviewer:
        return []
    has_where, has_behaviour = body_text_findings(body)
    reasons: list[str] = []
    if not has_where:
        reasons.append("body has no where_used")
    if not has_behaviour:
        reasons.append("body has no behaviour section")
    return reasons


# meiva:note
# Authorship trailers, read from the head commit messages AND the body. The squash merge keeps
# the body and drops the head message, and this repository records the trailer in the body ("only
# in a branch commit, the credit is lost"), so scanning commits alone reported "no trailer" for a
# pull request whose body ends with one (#8596). Scanning both only widens what counts, so the
# mistake cannot refuse a lane it used to accept.
def trailer_sets(messages: Iterable[str]) -> tuple[set[str], set[str]]:
    trailers: set[str] = set()
    harness: set[str] = set()
    for message in messages:
        for line in message.splitlines():
            lowered = line.lower().replace(" ", "")
            if lowered.startswith("co-authored-by:meiva#"):
                trailers.add(line.split("#")[-1].strip())
            if lowered.startswith(("co-authored-by:claude", "co-authored-by:codex")):
                harness.add(line.strip())
    return trailers, harness


# meiva:note
# The grant is data, not code: it changes as workers are spawned, so it lives beside whoever runs
# the gate (`MEIVA_LANE_FILE`) and is refreshed with the query recorded in that file, not with a
# pull request. A missing file used to fall back to a hard-coded pair of ids, which made the same
# gate answer differently on two machines without saying so -- one copy refused agents the other
# accepted. Absence is now an error, so a machine that cannot read the grant refuses instead of
# merging under a grant nobody wrote.
def granted_lanes(env_value: str | None, *, lane_file: Path = DEFAULT_LANE_FILE) -> set[str]:
    if env_value:
        lanes = {lane.strip() for lane in env_value.split(",") if lane.strip()}
    elif lane_file.is_file():
        lanes = {
            line.strip()
            for line in lane_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    else:
        raise MergeGateError(
            f"lane list missing at {lane_file}: set MEIVA_LANE_FILE to the grant file to read, "
            f"or MEIVA_LANE_AGENTS to a comma-separated list of agent ids"
        )
    if not lanes:
        raise MergeGateError(
            f"{lane_file} names no lane, so nothing could be merged: add the agent ids that may "
            f"merge, one per line, or set MEIVA_LANE_AGENTS"
        )
    return lanes


class PullRequest(NamedTuple):
    # meiva:note
    # The fields the five conditions read, decoded once.

    number: int
    full_name: str
    head_sha: str
    head_ref: str
    base_ref: str
    base_sha: str
    body: str
    title: str
    state: str
    merge_commit_sha: str
    mergeable: object
    mergeable_state: object


def load_pull_request(api: GhApi, pr: int) -> PullRequest:
    # meiva:note
    # `repos/{owner}/{repo}` is resolved by gh from the cwd's repository.
    repo = json_object(api.json("repos/{owner}/{repo}"), "repository")
    full_name = text_field(repo, "full_name", "repository")
    raw = json_object(api.json(f"repos/{full_name}/pulls/{pr}"), f"pull {pr}")
    head = json_object(raw.get("head"), "pull.head")
    base = json_object(raw.get("base"), "pull.base")
    body = raw.get("body")
    return PullRequest(
        number=pr,
        full_name=full_name,
        head_sha=text_field(head, "sha", "pull.head"),
        head_ref=text_field(head, "ref", "pull.head"),
        base_ref=text_field(base, "ref", "pull.base"),
        base_sha=text_field(base, "sha", "pull.base"),
        body=body if isinstance(body, str) else "",
        title=optional_text(raw, "title"),
        state=text_field(raw, "state", "pull"),
        merge_commit_sha=optional_text(raw, "merge_commit_sha"),
        mergeable=raw.get("mergeable"),
        mergeable_state=raw.get("mergeable_state"),
    )


def commit_messages(api: GhApi, full_name: str, pr: int) -> list[str]:
    rows = json_rows(api.json(f"repos/{full_name}/pulls/{pr}/commits"), "pulls.commits")
    messages: list[str] = []
    for index, raw in enumerate(rows):
        commit = json_object(raw, f"commit[{index}]")
        payload = commit.get("commit")
        message = optional_text(json_object(payload, "commit.commit"), "message") if payload else ""
        messages.append(message or optional_text(commit, "messageBody"))
    return messages


def compare_files(
    api: GhApi, full_name: str, base_sha: str, head_sha: str
) -> tuple[set[str], bool]:
    payload = json_object(api.json(f"repos/{full_name}/compare/{base_sha}...{head_sha}"), "compare")
    rows = json_rows(payload.get("files", []), "compare.files")
    names = {
        text_field(json_object(raw, f"compare.files[{index}]"), "filename", "compare file")
        for index, raw in enumerate(rows)
    }
    return names, len(rows) >= COMPARE_FILE_LIMIT


def report_header(pull: PullRequest) -> None:
    print(f"PR #{pull.number}  {pull.title}")
    print(f"repo     {pull.full_name}")
    print(f"head     {pull.head_sha}")
    print(f"base     {pull.base_ref}")
    print(
        f"state    {pull.state}  mergeable={pull.mergeable}  mergeable_state={pull.mergeable_state}"
    )


# meiva:note
# A closed or merged pull request must not fall through to the five conditions: `base..head`
# against a main that already contains the squash intersects every file, so the gate would say
# "rebase and re-run" for a pull request that is already in. Merged means the intent is
# satisfied; closed-unmerged means there is nothing to merge.
def closed_verdict(pull: PullRequest) -> int | None:
    if pull.state == "open":
        return None
    if pull.state == "closed" and pull.merge_commit_sha:
        print(f"\n{OK}: already merged as {pull.merge_commit_sha[:12]} -- nothing to do")
        return 0
    print(f"\n{FAIL}: PR is {pull.state} and not merged -- there is nothing to merge")
    return 1


def report_preflight(pull: PullRequest, *, reviewer: bool) -> str | None:
    print("\n[1] head-pinned preflight")
    line_ok = f"Preflight on head {pull.head_sha}:" in pull.body
    pass_ok = "PREFLIGHT PASS: /trunk merge is allowed" in pull.body
    print(f"    head line present: {line_ok} | PASS line present: {pass_ok}")
    printed = [line for line in pull.body.splitlines() if line.startswith("Preflight on head")]
    for line in printed[-3:]:
        print(f"    body: {line}")
    if reviewer and not (line_ok and pass_ok):
        print("    WAIVED: no preflight convention outside the lanes -- reviewed by hand")
    return preflight_verdict(pull.body, pull.head_sha, reviewer=reviewer)


def report_checks(api: GhApi, full_name: str, head_sha: str) -> str | None:
    print("\n[2] check runs")
    payload = json_object(
        api.json(f"repos/{full_name}/commits/{head_sha}/check-runs?per_page=100"), "check-runs"
    )
    all_runs = check_runs(json_rows(payload.get("check_runs", []), "check_runs"))
    latest = latest_by_name(all_runs)
    red = red_runs(latest.values())
    print(f"    {len(latest)} check runs, {len(red)} not green")
    for run in red:
        seen = sum(1 for candidate in all_runs if candidate.name == run.name)
        print(f"    RED {run.name}: {run.status}/{run.conclusion} (latest of {seen} run(s))")
    if not latest:
        return "no check runs on this head (nothing verified)"
    if red:
        return f"{len(red)} check run(s) not green"
    return None


def report_intersection(api: GhApi, pull: PullRequest) -> tuple[str, str | None]:
    print("\n[3] file intersection (PR base..head  x  base..current main)")
    head_files, head_truncated = compare_files(api, pull.full_name, pull.base_sha, pull.head_sha)
    main_sha = text_field(
        json_object(api.json(f"repos/{pull.full_name}/commits/{pull.base_ref}"), "commit"),
        "sha",
        "commit",
    )
    main_files, main_truncated = compare_files(api, pull.full_name, pull.base_sha, main_sha)
    shared = sorted(head_files & main_files)
    print(
        f"    pr files {len(head_files)} | main moved {len(main_files)} file(s) since base "
        f"(main now {main_sha[:10]})"
    )
    print(f"    intersection {len(shared)}")
    for name in shared[:20]:
        print(f"    SHARED {name}")
    verdict = intersection_verdict(shared, truncated=head_truncated or main_truncated)
    if verdict is None and shared:
        print(
            "    ALLOWED: shared files are only the owed list + generated region; merge then run "
            "`gen_pyright_test_environments.py --check` on main"
        )
    return main_sha, verdict


def report_body_text(pull: PullRequest, *, reviewer: bool) -> list[str]:
    print("\n[4] body text")
    has_where, has_behaviour = body_text_findings(pull.body)
    print(f"    where_used: {has_where} | behaviour section: {has_behaviour}")
    if reviewer:
        print("    WAIVED: where_used / behaviour section are lane conventions -- reviewed by hand")
    return body_text_reasons(pull.body, reviewer=reviewer)


def report_authorship(
    api: GhApi, pull: PullRequest, *, env: Mapping[str, str], reviewer: bool
) -> str | None:
    print("\n[5] lane authorship (grant = every worker 405 spawned)")
    lane_file = Path(env.get("MEIVA_LANE_FILE") or DEFAULT_LANE_FILE).expanduser().absolute()
    lanes = granted_lanes(env.get("MEIVA_LANE_AGENTS"), lane_file=lane_file)
    messages = [*commit_messages(api, pull.full_name, pull.number), pull.body]
    trailers, harness = trailer_sets(messages)
    # meiva:note
    # The coordinator handed its whole pyright line to 405, so the `meiva-pyright-any-*` ladder is
    # a lane 405 merges -- its commits carry the Claude harness trailer and no Meiva trailer, by
    # construction. Residual risk: a pull request opened by the user's own Claude Code session on
    # that same prefix would also pass.
    prefix = env.get("MEIVA_HANDED_OVER_PREFIX", "meiva-pyright-any-")
    handed_over = bool(prefix) and pull.head_ref.startswith(prefix)
    print(
        f"    Meiva trailers: {sorted(trailers) or 'none'} | "
        f"harness trailers: {sorted(harness) or 'none'} | branch {pull.head_ref} | "
        f"handed-over ladder: {handed_over}"
    )
    return lane_verdict(trailers, harness, lanes, handed_over=handed_over, reviewer=reviewer)


def report_verdict(reasons: list[str], *, reviewer: bool, head_sha: str, main_sha: str) -> int:
    print()
    if reasons:
        print(f"{FAIL}: " + "; ".join(reasons))
        return 1
    if reviewer:
        print(
            f"{OK} (reviewer merge): conditions 2 (checks) and 3 (file intersection) hold; "
            f"1/4/5 waived above -- head {head_sha[:12]}, main {main_sha[:12]}"
        )
    else:
        print(f"{OK}: four conditions hold (head {head_sha[:12]}, main {main_sha[:12]})")
    return 0


def main(pr: int, env: Mapping[str, str], *, gh: str) -> int:
    api = GhApi(gh)
    pull = load_pull_request(api, pr)
    reviewer = env.get("MEIVA_MERGE_REVIEWER") == "1"
    report_header(pull)
    closed = closed_verdict(pull)
    if closed is not None:
        return closed
    reasons: list[str] = []
    preflight = report_preflight(pull, reviewer=reviewer)
    if preflight:
        reasons.append(preflight)
    checks = report_checks(api, pull.full_name, pull.head_sha)
    if checks:
        reasons.append(checks)
    main_sha, intersection = report_intersection(api, pull)
    if intersection:
        reasons.append(intersection)
    reasons.extend(report_body_text(pull, reviewer=reviewer))
    authorship = report_authorship(api, pull, env=env, reviewer=reviewer)
    if authorship:
        reasons.append(authorship)
    return report_verdict(reasons, reviewer=reviewer, head_sha=pull.head_sha, main_sha=main_sha)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    try:
        pr = pr_number(sys.argv[1:])
        sys.exit(main(pr, os.environ, gh=real_gh(os.environ)))
    except MergeGateError as error:
        print(f"{FAIL}: {error}")
        sys.exit(1)
