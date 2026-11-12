# meiva:note
# Re-checks read published JUnit across shards: a test can move between shards
# on a later SHA. Absence is scoped to available bundles, never inferred from
# a workflow's green verdict. Missing, expired or malformed evidence stays unknown.
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path
from datetime import datetime

from base.clock import Deadline
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from schedules.plans.watch.junit import match_artifact
from schedules.plans.watch.junit_publications import (
    JUNIT_ARTIFACT_PREFIXES,
    attempt_started_at,
    current_publications,
)
from schedules.plans.watch.junit_cases import CaseObservation, CaseOutcome, collected_case_results
from schedules.plans.watch.main_red import Entry, RunFacts

RunProcess = Callable[..., subprocess.CompletedProcess[str]]
_REPO = "zhiyuan-zhang0206/Meiva"


class JunitReader:
    def __init__(
        self, scratch: Path, run_process: RunProcess, budget: Callable[[], Deadline]
    ) -> None:
        self.scratch = scratch
        self.run_process = run_process
        self.budget = budget
        self.deadline: Deadline | None = None
        self.cache: dict[
            tuple[int, int, str | None],
            tuple[dict[str, CaseOutcome], tuple[str, ...], tuple[str, ...]],
        ] = {}

    def _timeout(self) -> float:
        if self.deadline is None:
            self.deadline = self.budget()
        left = self.deadline.left()
        if left is None:
            raise TimeoutError("JUnit re-check round budget exhausted")
        return min(left, 180)

    def history(self, workflow: str) -> tuple[RunFacts, ...]:
        result = self.run_process(
            [
                "gh",
                "api",
                f"repos/{_REPO}/actions/workflows/{workflow}/runs?branch=main&per_page=10",
            ],
            capture_output=True,
            text=True,
            timeout=self._timeout(),
        )
        result.check_returncode()
        data = object_of(json.loads, result.stdout)
        rows = data.get("workflow_runs") if is_object_dict(data) else None
        if not is_object_list(rows):
            raise ValueError("run history has no workflow_runs array")
        facts: list[RunFacts] = []
        for row in rows:
            if not is_object_dict(row) or row.get("conclusion") not in {"success", "failure"}:
                continue
            identifier, created = row.get("id"), row.get("created_at")
            sha, conclusion, attempt = (
                row.get("head_sha"),
                row.get("conclusion"),
                row.get("run_attempt", 1),
            )
            if (
                not isinstance(identifier, int)
                or not isinstance(created, str)
                or not isinstance(sha, str)
                or not isinstance(conclusion, str)
                or not isinstance(attempt, int)
            ):
                raise TypeError("incomplete run history row")
            facts.append(
                RunFacts(
                    workflow,
                    sha,
                    datetime.fromisoformat(created.replace("Z", "+00:00")),
                    conclusion,
                    (),
                    run_id=identifier,
                    attempt=attempt,
                )
            )
        return tuple(facts)

    def __call__(self, entry: Entry, run: RunFacts) -> CaseObservation:
        job_scope = None if entry.covers_full_suite else entry.job
        key = (run.run_id, run.attempt, job_scope)
        if key not in self.cache:
            self.cache[key] = self._read(run, job_scope)
        cases, bundles, errors = self.cache[key]
        test_id = entry.fingerprint.partition("::")[2]
        outcome = cases.get(test_id)
        evidence = (
            f"run {run.run_id} attempt {run.attempt}, bundles: {', '.join(bundles) or 'none'}"
        )
        # meiva:note
        # A readable case cannot settle the review while the requested scope
        # contains unverified publications or unreadable XML: that publication
        # can contain the final verdict that would supersede a raw passing retry.
        if errors:
            return CaseObservation(CaseOutcome.UNREADABLE, f"{evidence}; {'; '.join(errors)}")
        if outcome is not None:
            if outcome == CaseOutcome.UNREADABLE:
                evidence += "; conflicting case results at the same publisher/retry"
            return CaseObservation(outcome, evidence)
        if not bundles:
            return CaseObservation(
                CaseOutcome.UNREADABLE, f"{evidence}; {'; '.join(errors) or 'no published JUnit'}"
            )
        return CaseObservation(
            CaseOutcome.NOT_COLLECTED,
            f"{test_id} absent from available JUnit ({evidence}); missing publications are not proof of execution",
        )

    def _read(
        self, run: RunFacts, job_scope: str | None
    ) -> tuple[dict[str, CaseOutcome], tuple[str, ...], tuple[str, ...]]:
        listing = self.run_process(
            ["gh", "api", f"repos/{_REPO}/actions/runs/{run.run_id}/artifacts?per_page=100"],
            capture_output=True,
            text=True,
            timeout=self._timeout(),
        )
        listing.check_returncode()
        data = object_of(json.loads, listing.stdout)
        artifacts = data.get("artifacts") if is_object_dict(data) else None
        if not is_object_list(artifacts):
            raise ValueError("artifact listing has no artifacts array")
        try:
            cutoff = self._attempt_start(run)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            return {}, (), (f"workflow attempt cannot be verified: {type(exc).__name__}: {exc}",)
        records = tuple(item for item in artifacts if is_object_dict(item))
        if job_scope is not None:
            records = tuple(
                item
                for item in records
                if isinstance(name := item.get("name"), str)
                and match_artifact(job_scope, (name,)).name is not None
            )
        by_name, publication_errors = current_publications(records, cutoff)
        if job_scope is not None:
            scoped = _scoped_publications(job_scope, by_name)
            if not scoped:
                return (
                    {},
                    (),
                    (
                        f"no unambiguous current JUnit for partial lane {job_scope}",
                        *publication_errors,
                    ),
                )
            by_name = scoped
        cases: dict[str, CaseOutcome] = {}
        versions: dict[str, tuple[int, int]] = {}
        bundles: list[str] = []
        errors = list(publication_errors)
        for name, identifier in by_name.items():
            try:
                documents = self._documents(identifier)
                if not documents:
                    errors.append(f"{name}: no XML documents")
                    continue
                for internal_attempt, document in documents:
                    results = collected_case_results(document)
                    if results is None:
                        errors.append(f"{name}: unreadable JUnit")
                        continue
                    bundles.append(name)
                    for result in results:
                        # meiva:note
                        # Verdict bundles describe the publisher's final adjudication;
                        # raw -aN files are retries inside one workflow attempt. Later
                        # retries supersede earlier case results, not unrelated cases.
                        version = (int(name.startswith("verdict-junit-")), internal_attempt)
                        previous = cases.get(result.test_id)
                        previous_version = versions.get(result.test_id)
                        if previous_version is None or version > previous_version:
                            cases[result.test_id] = result.outcome
                            versions[result.test_id] = version
                        elif version == previous_version and previous != result.outcome:
                            cases[result.test_id] = CaseOutcome.UNREADABLE
            except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        return cases, tuple(dict.fromkeys(bundles)), tuple(errors)

    def _attempt_start(self, run: RunFacts) -> datetime:
        answer = self.run_process(
            ["gh", "api", f"repos/{_REPO}/actions/runs/{run.run_id}"],
            capture_output=True,
            text=True,
            timeout=self._timeout(),
        )
        answer.check_returncode()
        metadata = object_of(json.loads, answer.stdout)
        if not is_object_dict(metadata):
            raise ValueError("workflow attempt metadata is not an object")
        return attempt_started_at(metadata, run.attempt)

    def _documents(self, artifact_id: int) -> tuple[tuple[int, str], ...]:
        self.scratch.mkdir(parents=True, exist_ok=True)
        path = self.scratch / f"recheck-artifact-{artifact_id}.zip"
        try:
            with path.open("wb") as sink:
                result = self.run_process(
                    ["gh", "api", f"repos/{_REPO}/actions/artifacts/{artifact_id}/zip"],
                    stdout=sink,
                    stderr=subprocess.PIPE,
                    timeout=self._timeout(),
                )
            result.check_returncode()
            with zipfile.ZipFile(path) as bundle:
                return tuple(
                    (_internal_attempt(name), bundle.read(name).decode("utf-8", "replace"))
                    for name in bundle.namelist()
                    if name.endswith(".xml")
                )
        finally:
            path.unlink(missing_ok=True)


# meiva:note
# This suffix belongs to pytest retries within one job (ci.yml), not to the
# workflow run_attempt. Workflow freshness is checked against artifact time.
def _internal_attempt(name: str) -> int:
    match = re.search(r"-a(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


# meiva:note
# Partial lanes can publish both raw retries and a final verdict. Select each
# publisher independently so an exact raw name cannot hide its final verdict;
# ambiguity within one publisher still makes this lane unreadable.
def _scoped_publications(job: str, by_name: dict[str, int]) -> dict[str, int]:
    selected: dict[str, int] = {}
    for prefix in JUNIT_ARTIFACT_PREFIXES:
        match = match_artifact(job, tuple(name for name in by_name if name.startswith(prefix)))
        if match.name is not None:
            selected[match.name] = by_name[match.name]
        elif match.candidates:
            return {}
    return selected
