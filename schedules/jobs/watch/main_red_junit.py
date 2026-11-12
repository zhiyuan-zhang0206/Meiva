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
        if outcome is not None:
            return CaseObservation(outcome, evidence)
        if errors or not bundles:
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
        if job_scope is not None:
            names = tuple(
                name
                for item in artifacts
                if is_object_dict(item)
                for name in (item.get("name"),)
                if isinstance(name, str)
            )
            chosen = match_artifact(job_scope, names)
            if chosen.name is None:
                return {}, (), (f"no published JUnit for partial lane {job_scope}",)
            artifacts = [
                item
                for item in artifacts
                if is_object_dict(item) and item.get("name") == chosen.name
            ]
        cases: dict[str, CaseOutcome] = {}
        bundles: list[str] = []
        errors: list[str] = []
        for artifact in artifacts:
            if not is_object_dict(artifact):
                continue
            name, identifier = artifact.get("name"), artifact.get("id")
            if not isinstance(name, str) or not name.startswith("test-results-"):
                continue
            if not isinstance(identifier, int) or artifact.get("expired") is True:
                errors.append(f"{name}: missing id or expired")
                continue
            try:
                documents = self._documents(identifier, run.attempt)
                if not documents:
                    errors.append(f"{name}: no XML documents")
                    continue
                for document in documents:
                    results = collected_case_results(document)
                    if results is None:
                        errors.append(f"{name}: unreadable JUnit")
                        continue
                    bundles.append(name)
                    for result in results:
                        # meiva:note
                        # Conflicting retries in one publication do not prove green;
                        # any failure wins, and skip outranks pass without ordering evidence.
                        rank = {
                            CaseOutcome.PASSED: 0,
                            CaseOutcome.SKIPPED: 1,
                            CaseOutcome.FAILED: 2,
                        }
                        previous = cases.get(result.test_id)
                        if previous is None or rank[result.outcome] > rank[previous]:
                            cases[result.test_id] = result.outcome
            except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        return cases, tuple(dict.fromkeys(bundles)), tuple(errors)

    def _documents(self, artifact_id: int, attempt: int) -> tuple[str, ...]:
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
                    bundle.read(name).decode("utf-8", "replace")
                    for name in bundle.namelist()
                    if name.endswith(".xml") and _belongs_to_attempt(name, attempt)
                )
        finally:
            path.unlink(missing_ok=True)


# meiva:note
# Backend publishers encode the attempt in the XML filename. A re-run's old
# artifact must not supply the first attempt's red as if it were fresh evidence.
def _belongs_to_attempt(name: str, attempt: int) -> bool:
    match = re.search(r"-a(\d+)\.xml$", name)
    return match is None or int(match.group(1)) == attempt
