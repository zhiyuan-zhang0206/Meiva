#!/usr/bin/env python3
# meiva:note
# The rule behind `e2e gate`: a red the pull request did not cause must not block it, and a red it
# did cause must still block. The nightly run on `main` is the only authority for "already red":
# its failing test identities are the inherited set, and each one is declared with the card that
# owns it and the date the excuse expires. A pull request whose `e2e` failures are all inherited
# passes and says so in the check summary; anything else fails.
#
# Four ways this could become a silent green, each closed by failing instead:
#   - the nightly's artifacts cannot be read   -> red ("no authoritative set"), never "empty"
#   - the last completed nightly is too old    -> red
#   - the nightly head is not an ancestor of
#     the pull request base                    -> red ("the base lacks what it tested")
#   - the run's own failing cases cannot be
#     read from the attempt that failed        -> red, never "nothing failed"
#
# Identities, not job names: one test runs in `e2e shard (3/4)` on one workflow and
# `e2e full matrix (3/4)` on another, so a job-name set reads one red as two.
#
# Artifacts are anchored to the attempt that produced the run's verdict: two attempts of one run
# upload artifacts under the same name, and a name-only fetch resolves to the newest one, which is
# how a rerun turns a red set into an empty set that looks correct.
#
# It lives with the watch (`schedules/jobs/watch/`) because the fingerprint it decides on is the
# watch's own, in `schedules/plans/watch/junit.py`, and `scripts` may not import `schedules`:
# `schedules` already imports `scripts`, so that edge would close a cycle. `ci.yml`'s `e2e gate`
# job runs it as `python -m schedules.jobs.watch.inherited_reds`.
# meiva:link scripts/ci/docs/inherited-reds.meiva.okf.md

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.options import options
from base.host.boundary.untyped import object_of
from schedules.plans.watch.junit import artifact_name_for_job, failing_case_ids

__all__ = [
    "Declaration",
    "DeclarationError",
    "DeclaredRed",
    "GitHub",
    "Nightly",
    "Options",
    "Verdict",
    "failed_identities",
    "judge",
    "load_declaration",
    "main",
    "result_artifact_ids",
]

# meiva:note
# The nightly is daily and its phase has drifted by 4.9-5.5 hours; 30 hours is one cadence plus
# that drift, so an older run means the watch itself is broken and the excuse has no authority.
MAX_NIGHTLY_AGE = timedelta(hours=30)

# meiva:note
# An artifact is uploaded at the end of its job, so it is matched inside the job's own window with
# a margin for the upload step itself.
_UPLOAD_SLACK = timedelta(minutes=5)

_NIGHTLY_WORKFLOW = "ci.yml"
_RESULT_ARTIFACT = re.compile(r"^test-results-e2e-shard-\d+$")
_SEPARATOR = "::"


# meiva:note
# Nothing can be excused: the authoritative set is missing, stale or untrustworthy.
class DeclarationError(ValueError):
    pass


@dataclass(frozen=True)
class DeclaredRed:
    nodeid: str
    card: str
    expires: date

    def describe(self) -> str:
        return f"{self.nodeid} (card {self.card}, expires {self.expires.isoformat()})"


@dataclass(frozen=True)
class Declaration:
    reds: tuple[DeclaredRed, ...]


@dataclass(frozen=True)
class Nightly:
    run_id: int
    head_sha: str
    attempt: int
    completed_at: datetime
    conclusion: str

    @property
    def failed(self) -> bool:
        return self.conclusion == "failure"


@dataclass(frozen=True)
class Options:
    classify: str
    suite: str
    base_sha: str
    repository: str
    run_id: int
    run_attempt: int
    branch: str
    declaration: Path


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    inherited: tuple[DeclaredRed, ...] = ()
    fresh: tuple[str, ...] = ()
    nightly: Nightly | None = None
    nightly_is_ancestor: bool | None = None

    # meiva:note
    # A pass is an event, not an absence: the summary names the run it trusted, how old that run
    # is, whether this base contains it, and every red it waved through with its card and expiry.
    def summary(self, *, now: datetime) -> str:
        lines = [f"e2e gate: {'PASS' if self.passed else 'FAIL'} - {self.reason}"]
        if self.nightly is not None:
            hours = (now - self.nightly.completed_at).total_seconds() / 3600
            lines.append(
                f"nightly: run {self.nightly.run_id} attempt {self.nightly.attempt} "
                f"sha {self.nightly.head_sha[:12]} "
                f"completed {self.nightly.completed_at.isoformat()} ({hours:.1f}h ago)"
            )
            if self.nightly_is_ancestor is not None:
                lines.append(
                    "nightly head is an ancestor of this base: "
                    + ("yes" if self.nightly_is_ancestor else "no")
                )
        lines.append(f"inherited red {len(self.inherited)}:")
        lines.extend(f"  - {red.describe()}" for red in self.inherited)
        if self.fresh:
            lines.append(f"new red {len(self.fresh)}:")
            lines.extend(f"  - {nodeid}" for nodeid in self.fresh)
        return "\n".join(lines)


# meiva:note
# Every entry names the card that owns the red and the date the excuse dies; a set that cannot be
# read is not an empty set, so a malformed declaration refuses the gate instead of excusing nothing.
def load_declaration(text: str) -> Declaration:
    try:
        document = object_of(tomllib.loads, text)
    except tomllib.TOMLDecodeError as error:
        raise DeclarationError(f"the declared red set is not valid TOML: {error}") from error
    if not is_object_dict(document):
        raise DeclarationError("declared red set is not a TOML table")
    entries = document.get("inherited_red")
    if not is_object_list(entries):
        raise DeclarationError("declared red set has no [[inherited_red]] entries")
    reds: list[DeclaredRed] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        red = _declared_red(index, entry)
        if red.nodeid in seen:
            raise DeclarationError(f"inherited_red[{index}] repeats {red.nodeid}")
        seen.add(red.nodeid)
        reds.append(red)
    return Declaration(reds=tuple(reds))


def _declared_red(index: int, entry: object) -> DeclaredRed:
    if not is_object_dict(entry):
        raise DeclarationError(f"inherited_red[{index}] is not a table")
    nodeid = entry.get("nodeid")
    card = entry.get("card")
    expires = entry.get("expires")
    if not isinstance(nodeid, str) or _SEPARATOR not in nodeid:
        raise DeclarationError(
            f"inherited_red[{index}].nodeid must be '<file>::<test name>', got {nodeid!r}"
        )
    if not isinstance(card, str) or not card.startswith("#"):
        raise DeclarationError(
            f"inherited_red[{index}].card must name the card that owns the red, got {card!r}"
        )
    # meiva:note
    # A TOML datetime is a `date` too, and comparing one with today's date raises later, where the
    # cause is no longer readable: refuse it here.
    if not isinstance(expires, date) or isinstance(expires, datetime):
        raise DeclarationError(
            f"inherited_red[{index}].expires must be a TOML date, got {expires!r}"
        )
    return DeclaredRed(nodeid=nodeid, card=card, expires=expires)


# meiva:note
# The identity format is the watch's own: `<file>::<name>`, owned by `schedules/plans/watch/junit.py`,
# so a test keeps one fingerprint whether the log or an artifact named it. Minting a second format
# here would split the set this gate excuses from the set the watch reports.
def failed_identities(document: bytes) -> frozenset[str]:
    return frozenset(failing_case_ids(document.decode("utf-8", "replace")))


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# meiva:note
# One artifact belongs to one job of one attempt: its name says which job (derived by the watch's
# own `artifact_name_for_job`, the owner of that mapping) and its creation time says which attempt.
# A job that passed contributes no window, so its artifact is never read -- nothing failed in it.
def _failing_job_windows(jobs: Sequence[object]) -> dict[str, tuple[datetime, datetime]]:
    windows: dict[str, tuple[datetime, datetime]] = {}
    for job in jobs:
        if not is_object_dict(job) or job.get("conclusion") != "failure":
            continue
        job_name = job.get("name")
        started = _timestamp(job.get("started_at"))
        completed = _timestamp(job.get("completed_at"))
        if not isinstance(job_name, str) or started is None or completed is None:
            continue
        windows[artifact_name_for_job(job_name)] = (started, completed + _UPLOAD_SLACK)
    return windows


# meiva:note
# An artifact carries no attempt field, so the attempt is recovered from the job window it was
# uploaded inside. Selecting by name alone would return another attempt's artifact — a rerun's
# green one — which is how a red set becomes empty and looks correct.
def result_artifact_ids(artifacts: Sequence[object], jobs: Sequence[object]) -> tuple[int, ...]:
    windows = _failing_job_windows(jobs)
    selected: list[int] = []
    for artifact in artifacts:
        if not is_object_dict(artifact):
            continue
        name = artifact.get("name")
        created = _timestamp(artifact.get("created_at"))
        artifact_id = artifact.get("id")
        if not isinstance(name, str) or not _RESULT_ARTIFACT.match(name):
            continue
        window = windows.get(name)
        if window is None or created is None or not isinstance(artifact_id, int):
            continue
        if window[0] <= created <= window[1]:
            selected.append(artifact_id)
    return tuple(sorted(selected))


# meiva:note
# `gh` as a subprocess: bytes out, so a zip and a JSON document share one shape.
class GitHub:
    def __init__(self, run: Callable[..., subprocess.CompletedProcess[bytes]]) -> None:
        self._run = run

    def api(self, path: str) -> bytes:
        result = self._run(["gh", "api", path], timeout=120.0)
        if result.returncode:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise DeclarationError(f"gh api {path} failed: {stderr}")
        return result.stdout

    def json(self, path: str) -> object:
        return object_of(json.loads, self.api(path).decode("utf-8"))

    def paged(self, path: str, key: str) -> list[object]:
        rows: list[object] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            document = self.json(f"{path}{separator}per_page=100&page={page}")
            if not is_object_dict(document):
                raise DeclarationError(f"{path} did not answer with an object")
            batch = document.get(key)
            if not is_object_list(batch):
                raise DeclarationError(f"{path} carries no {key} list")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            page += 1


def _junit_documents(archive: bytes) -> list[bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            return [
                bundle.read(name)
                for name in bundle.namelist()
                if name.endswith(".xml") and not name.endswith("/")
            ]
    except zipfile.BadZipFile:
        raise DeclarationError("an artifact download was not a zip archive") from None


def failures_of_attempt(
    api: GitHub, *, repository: str, run_id: int, attempt: int
) -> frozenset[str]:
    jobs = api.paged(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    artifacts = api.paged(f"repos/{repository}/actions/runs/{run_id}/artifacts", "artifacts")
    identities: set[str] = set()
    for artifact_id in result_artifact_ids(artifacts, jobs):
        archive = api.api(f"repos/{repository}/actions/artifacts/{artifact_id}/zip")
        for document in _junit_documents(archive):
            identities |= failed_identities(document)
    return frozenset(identities)


# meiva:note
# The nightly whose verdict is final, read at its own attempt: a rerun's green attempt is the run's
# answer, so the reds of the attempt it replaced are history, not the current set.
def completed_nightly(api: GitHub, *, repository: str, branch: str) -> Nightly:
    runs = api.paged(
        f"repos/{repository}/actions/workflows/{_NIGHTLY_WORKFLOW}/runs?branch={branch}"
        "&event=schedule",
        "workflow_runs",
    )
    for run in runs:
        nightly = _nightly(run)
        if nightly is not None:
            return nightly
    raise DeclarationError("no completed nightly run is available on the base branch")


def _nightly(run: object) -> Nightly | None:
    if not is_object_dict(run):
        return None
    if run.get("status") != "completed":
        return None
    conclusion = run.get("conclusion")
    if conclusion not in {"success", "failure"}:
        return None
    run_id = run.get("id")
    head_sha = run.get("head_sha")
    attempt = run.get("run_attempt")
    completed_at = _timestamp(run.get("updated_at"))
    if not isinstance(run_id, int) or not isinstance(head_sha, str):
        return None
    if not isinstance(attempt, int) or completed_at is None:
        return None
    return Nightly(
        run_id=run_id,
        head_sha=head_sha,
        attempt=attempt,
        completed_at=completed_at,
        conclusion=str(conclusion),
    )


# meiva:note
# Ancestry is the graph's answer, not the clock's, and the direction is the one that can fire: the
# nightly head must be an ancestor of this pull request's base, so the base contains the state the
# nightly tested. Read the other way round it could never hold -- the pull request that declares an
# inherited red is merged after the nightly, so `main` is already past that head by the time any
# later pull request asks (405, 2026-11-08). `compare(nightly_head...base)` answering `ahead` or
# `identical` is exactly that; `behind` and `diverged` mean the base lacks commits the nightly saw.
def nightly_is_ancestor(api: GitHub, *, repository: str, nightly_head: str, base_sha: str) -> bool:
    document = api.json(f"repos/{repository}/compare/{nightly_head}...{base_sha}")
    if not is_object_dict(document):
        raise DeclarationError("the comparison did not answer with an object")
    return document.get("status") in {"ahead", "identical"}


def judge(
    *,
    observed: frozenset[str],
    nightly_failures: frozenset[str],
    declaration: Declaration,
    nightly: Nightly,
    nightly_is_ancestor: bool,
    now: datetime,
) -> Verdict:
    if not nightly_is_ancestor:
        return Verdict(
            passed=False,
            reason=(
                "the nightly head is not an ancestor of the pull request base, so the nightly set "
                "describes a state this base does not contain"
            ),
            nightly=nightly,
            nightly_is_ancestor=False,
        )
    age = now - nightly.completed_at
    if age > MAX_NIGHTLY_AGE:
        return Verdict(
            passed=False,
            reason=(
                f"the last completed nightly is {age.total_seconds() / 3600:.1f}h old "
                f"(limit {MAX_NIGHTLY_AGE})"
            ),
            nightly=nightly,
            nightly_is_ancestor=True,
        )
    included = {
        red.nodeid
        for red in declaration.reds
        if red.nodeid in observed and red.nodeid in nightly_failures and red.expires >= now.date()
    }
    inherited = tuple(
        sorted((red for red in declaration.reds if red.nodeid in included), key=_by_nodeid)
    )
    fresh = tuple(sorted(observed - included))
    if fresh:
        return Verdict(
            passed=False,
            reason=f"{len(fresh)} failure(s) are not inherited",
            inherited=inherited,
            fresh=fresh,
            nightly=nightly,
            nightly_is_ancestor=True,
        )
    return Verdict(
        passed=True,
        reason=f"every failure is inherited from run {nightly.run_id}",
        inherited=inherited,
        nightly=nightly,
        nightly_is_ancestor=True,
    )


def _by_nodeid(red: DeclaredRed) -> str:
    return red.nodeid


def _run(argv: Sequence[str], /, *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    # meiva:note
    # Fixed argv: the program is `gh`, the path is built from this repository's own ids, and no
    # argument comes from a pull request's text. Bytes out, so a zip download and a JSON answer
    # share one shape.
    return subprocess.run(list(argv), check=False, capture_output=True, timeout=timeout)


# meiva:note
# A run counter from the workflow environment; unset reads as 0 and fails at the API.
def _number(environ: Mapping[str, str], name: str) -> int:
    value = environ.get(name, "").strip()
    return int(value) if value else 0


# meiva:note
# The flags restate what the workflow already knows, so a missing one is caught here rather than
# read as an empty set: `options` refuses a namespace whose shape the dataclass disagrees with.
def _options(argv: Sequence[str], environ: Mapping[str, str]) -> Options:
    parser = argparse.ArgumentParser(description="Excuse only the reds main already has.")
    parser.add_argument("--classify", default="")
    parser.add_argument("--suite", default="")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--repository", default=environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", type=int, default=_number(environ, "GITHUB_RUN_ID"))
    parser.add_argument("--run-attempt", type=int, default=_number(environ, "GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--declaration",
        type=Path,
        default=Path(__file__).with_name("inherited-reds.toml"),
    )
    return options(parser.parse_args(list(argv)), Options)


# meiva:note
# The gate's entry: every value it reads from the world arrives from the guard at the bottom of this
# file, so `main` holds no default that would reach `gh` on its own.
def main(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    now: datetime,
    api: GitHub,
    declaration_text: str | None = None,
) -> int:
    options = _options(argv, environ)
    if options.classify != "success":
        print(f"e2e gate: FAIL - classify is {options.classify or 'missing'}")
        return 1
    if options.suite in {"success", "skipped"}:
        print(f"e2e gate: PASS - the suite is {options.suite}")
        return 0
    if not options.base_sha:
        print(
            f"e2e gate: FAIL - the suite is {options.suite} and this run has no pull request base"
        )
        return 1
    try:
        return _decide(
            api,
            options=options,
            now=now,
            declaration_text=declaration_text,
        )
    except DeclarationError as error:
        print(f"e2e gate: FAIL - no authoritative red set: {error}")
        return 1


# meiva:note
# A file that cannot be read is a set that cannot be read, not a set with no entries.
def _declaration_text(path: Path, given: str | None) -> str:
    if given is not None:
        return given
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise DeclarationError(f"the declaration at {path} could not be read: {error}") from error


def _decide(
    api: GitHub,
    *,
    options: Options,
    now: datetime,
    declaration_text: str | None,
) -> int:
    declaration = load_declaration(_declaration_text(options.declaration, declaration_text))
    observed = failures_of_attempt(
        api, repository=options.repository, run_id=options.run_id, attempt=options.run_attempt
    )
    if not observed:
        print(
            f"e2e gate: FAIL - the suite is {options.suite} but no failing test identity could be "
            "read from its attempt"
        )
        return 1
    nightly = completed_nightly(api, repository=options.repository, branch=options.branch)
    nightly_failures = (
        failures_of_attempt(
            api, repository=options.repository, run_id=nightly.run_id, attempt=nightly.attempt
        )
        if nightly.failed
        else frozenset[str]()
    )
    verdict = judge(
        observed=observed,
        nightly_failures=nightly_failures,
        declaration=declaration,
        nightly=nightly,
        nightly_is_ancestor=nightly_is_ancestor(
            api,
            repository=options.repository,
            nightly_head=nightly.head_sha,
            base_sha=options.base_sha,
        ),
        now=now,
    )
    print(verdict.summary(now=now))
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1:],
            environ=os.environ,
            now=datetime.now().astimezone(),
            api=GitHub(_run),
        )
    )
