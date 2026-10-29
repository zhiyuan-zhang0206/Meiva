from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-gate.yml"


def _yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def test_only_a_launched_wave_starts_the_gate() -> None:
    workflow = _yaml(_WORKFLOW)

    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow[True]["workflow_dispatch"]["inputs"]["version"]["required"] is True


def test_every_leg_runs_on_the_commit_the_precheck_resolved() -> None:
    jobs = _yaml(_WORKFLOW)["jobs"]

    assert set(jobs) == {"precheck", "backend", "e2e", "platform-smoke", "certify"}
    for name in ("backend", "e2e", "platform-smoke"):
        assert jobs[name]["needs"] == "precheck"
        checkout = jobs[name]["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.precheck.outputs.sha }}"


def test_the_precheck_refuses_a_tree_without_a_certified_base() -> None:
    precheck = _yaml(_WORKFLOW)["jobs"]["precheck"]
    refusal = _step(precheck["steps"], "Refuse a tree without a certified base")

    assert "scripts/host_ops/release/base.py" in refusal["run"]


def test_only_a_tree_that_passed_every_leg_is_certified() -> None:
    jobs = _yaml(_WORKFLOW)["jobs"]
    certify = jobs["certify"]

    assert certify["needs"] == ["precheck", "backend", "e2e", "platform-smoke"]


def test_certification_writes_the_record_and_the_tag_that_points_at_it() -> None:
    certify = _yaml(_WORKFLOW)["jobs"]["certify"]
    record = _step(certify["steps"], "Write the release record")
    tag = _step(certify["steps"], "Publish the tag the humans read")
    pull_request = _step(certify["steps"], "Open the pull request that lands the record")

    assert '"passed": True' in record["run"]
    assert 'path = pathlib.Path("release") / f"{version}.json"' in record["run"]
    assert '"v${{ inputs.version }}"' in tag["run"]
    assert tag["run"].index("git push origin") > tag["run"].index("git tag -a")
    assert "gh pr create" in pull_request["run"]


def test_a_red_night_blocks_the_gate_and_a_missing_night_does_not() -> None:
    precheck = _yaml(_WORKFLOW)["jobs"]["precheck"]
    nightly = _step(precheck["steps"], "Refuse a commit whose nightly full suite was red")
    script = nightly["run"]

    assert "refresh-test-durations.yml" in script
    assert 'failure) echo "::error::' in script
    assert "exit 1" in script.split("failure)")[1].split(";;")[0]
    assert "no nightly run for $sha" in script
