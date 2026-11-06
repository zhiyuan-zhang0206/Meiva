# meiva:note
# Every workflow's trigger set is declared here, and a change to any of them fails this file.
# meiva:note
# Why a table and not a rule: on 2026-10-31 `ci.yml` lost its `pull_request` trigger, and on
# 2026-11-01 the documentation that still claimed it had one became the reason a merge gate was
# retired -- the reader trusted the stale copy, and the repository spent a day with no pull-request
# CI at all. A document cannot be kept in sync by willpower; what the trigger set is has to be a
# value somebody must edit on purpose, in a diff a reviewer sees. This table is that value. Adding
# a workflow, removing one, or changing any `on:` block turns this test red, and the fix is to say
# out loud what changed -- not to teach the test to accept it.
# meiva:link .github/ci-workflow.meiva.okf.md
from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"

DECLARED: dict[str, frozenset[str]] = {
    "attribution-gate.yml": frozenset({"pull_request"}),
    "audit-branch-protection.yml": frozenset({"schedule", "workflow_dispatch"}),
    "cache-warm.yml": frozenset({"push", "workflow_dispatch"}),
    "ci-app.yml": frozenset({"pull_request", "push", "workflow_dispatch"}),
    "ci-rerun.yml": frozenset({"workflow_run"}),
    "ci-runner-image.yml": frozenset({"pull_request", "push", "workflow_dispatch"}),
    "ci.yml": frozenset({"pull_request", "schedule", "workflow_dispatch"}),
    "dependency-audit.yml": frozenset({"schedule", "workflow_dispatch"}),
    "e2e-owned-port.yml": frozenset({"pull_request"}),
    "issue-router.yml": frozenset({"issues"}),
    "main-push.yml": frozenset({"push"}),
    "main-typecheck.yml": frozenset({"workflow_dispatch"}),
    "native-root-lifetime-proof.yml": frozenset({"pull_request", "workflow_dispatch"}),
    "promote-release.yml": frozenset({"workflow_dispatch"}),
    "refresh-test-durations.yml": frozenset({"schedule", "workflow_dispatch"}),
    "release-android.yml": frozenset({"push", "workflow_dispatch"}),
    "release-app.yml": frozenset({"push", "workflow_dispatch"}),
    "release-gate.yml": frozenset({"schedule", "workflow_dispatch", "pull_request"}),
    "secret-scan-history.yml": frozenset({"schedule", "workflow_dispatch"}),
    "update-model-pricing.yml": frozenset({"schedule", "workflow_dispatch"}),
}


def triggers(workflow: Path) -> frozenset[str]:
    # meiva:note
    # The `on:` block in three shapes: a bare event name, a list, or a mapping whose keys are the
    # events. YAML 1.1 reads an unquoted `on` as the boolean True, which is why both spellings are
    # looked up.
    document = cast("dict[object, object]", yaml.safe_load(workflow.read_text(encoding="utf-8")))
    declared = document["on" if "on" in document else True]
    if isinstance(declared, str):
        return frozenset({declared})
    if isinstance(declared, list):
        listed = cast("list[object]", declared)
        return frozenset(str(event) for event in listed)
    if isinstance(declared, dict):
        mapped = cast("dict[object, object]", declared)
        return frozenset(str(event) for event in mapped)
    raise AssertionError(f"{workflow.name}: unrecognised `on:` block {declared!r}")


def test_every_workflow_is_declared_here() -> None:
    on_disk = {path.name for path in WORKFLOWS.glob("*.yml")}

    added = sorted(on_disk - set(DECLARED))
    removed = sorted(set(DECLARED) - on_disk)

    assert not added, f"new workflows with no declared trigger set: {added}"
    assert not removed, f"declared workflows that no longer exist: {removed}"


@pytest.mark.parametrize("name", sorted(DECLARED))
def test_the_declared_triggers_are_the_ones_in_the_file(name: str) -> None:
    actual = triggers(WORKFLOWS / name)

    assert actual == DECLARED[name], (
        f"{name}: the file triggers on {sorted(actual)}, this table says {sorted(DECLARED[name])}; "
        "changing when a workflow runs is a decision -- update this table in the same commit "
        "and say why in the pull request"
    )
