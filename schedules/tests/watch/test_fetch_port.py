# meiva:note
# The fetch port has two halves in two files: `watch_once` calls `fetch(workflow, seen_at,
# seen_attempt)` positionally, and the schedule entry composes the port with
# `partial(fetch_runs, run_process=..., scratch=...)`. #8677 put the watermark behind the two effect
# parameters, so every call raised `TypeError: fetch_runs() got multiple values for argument
# 'run_process'`, and the first wave carrying it reported four unreadable streams per round
# (2026-11-08). Each half had a test; nothing tested the binding.
#
# The check is structural on purpose: the entry file has a name no `import` can reach, and the
# property that broke is the parameter *order*, which the AST states exactly. Loading the module
# dynamically would need an allowance in `meizel.policy.toml` for a test that reads one signature.
# meiva:link schedules/README.md

from __future__ import annotations

import ast
from pathlib import Path

_ENTRY = Path(__file__).resolve().parents[2] / "jobs" / "watch" / "main-red-watch-schedule.py"
_LOOP = Path(__file__).resolve().parents[2] / "jobs" / "watch" / "main_red_watch.py"


def _function(path: Path, name: str) -> ast.FunctionDef:
    module = ast.parse(path.read_text())
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def test_the_fetch_signature_is_the_ports_shape() -> None:
    fetch_runs = _function(_ENTRY, "fetch_runs")

    assert [arg.arg for arg in fetch_runs.args.args][:3] == ["workflow", "seen_at", "seen_attempt"]
    assert [arg.arg for arg in fetch_runs.args.kwonlyargs] == ["run_process", "scratch"]
    defaults = [ast.unparse(item) for item in fetch_runs.args.defaults]
    assert defaults == ["None", "None"], "the watermark must default to None for the first round"


def test_the_loop_hands_the_watermark_positionally() -> None:
    calls = [
        node
        for node in ast.walk(_function(_LOOP, "watch_once"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "fetch"
    ]

    assert len(calls) == 1
    assert len(calls[0].args) == 3
    assert calls[0].keywords == []


def test_runtime_composes_the_case_reader_for_real_rounds() -> None:
    calls = [
        node
        for node in ast.walk(_function(_ENTRY, "watch_round"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "watch_once"
    ]
    assert len(calls) == 1
    reader = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "read_case")
    assert isinstance(reader, ast.Name) and reader.id == "case_reader"
    constructors = [
        node
        for node in ast.walk(_function(_ENTRY, "watch_round"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "JunitReader"
    ]
    assert len(constructors) == 1


def test_initial_failure_reader_carries_workflow_attempt_to_publication_selection() -> None:
    function = _function(_ENTRY, "junit_case_ids")
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert any(
        isinstance(node.func, ast.Name) and node.func.id == "current_publications" for node in calls
    )
    fallback = _function(_ENTRY, "_job_failures")
    reads = [
        node
        for node in ast.walk(fallback)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "junit_case_ids"
    ]
    assert len(reads) == 1
    assert any(keyword.arg == "attempt" for keyword in reads[0].keywords)


def test_slot_entry_reads_only_verified_scheduled_history() -> None:
    calls = [
        node
        for node in ast.walk(_function(_ENTRY, "_slot_note"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "read_scheduled_starts"
    ]
    assert len(calls) == 1
    assert ast.unparse(calls[0].args[1]) == "_RELEASE_GATE"
    assert any(
        k.arg == "branch" and isinstance(k.value, ast.Constant) and k.value.value == "main"
        for k in calls[0].keywords
    )


# meiva:link docs/history/2026-11-15/scheduled-audits-gitleaks-watch.md
def test_scheduled_audits_is_watched_with_liveness_and_per_job_greens() -> None:
    module = ast.parse(_ENTRY.read_text())
    constants = {
        target.id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    assert constants["_SCHEDULED_AUDITS"] == "scheduled-audits.yml"

    full_suite_assign = next(
        node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_FULL_SUITE_WORKFLOWS" for t in node.targets)
    )
    assert "_SCHEDULED_AUDITS" not in ast.unparse(full_suite_assign)

    watch_calls = [
        node
        for node in ast.walk(_function(_ENTRY, "watch_round"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "watch_once"
    ]
    assert len(watch_calls) == 1
    workflows_kw = next(kw.value for kw in watch_calls[0].keywords if kw.arg == "workflows")
    liveness_kw = next(kw.value for kw in watch_calls[0].keywords if kw.arg == "liveness")
    assert "_SCHEDULED_AUDITS" in ast.unparse(workflows_kw)
    assert (
        "LivenessStream('scheduled-audits', _SCHEDULED_AUDITS, NIGHTLY_STALE_AFTER)"
        in ast.unparse(liveness_kw)
    )

    fetch_body = ast.unparse(_function(_ENTRY, "fetch_runs"))
    assert "AUDIT_CHECK_STEPS" in fetch_body
    assert "RunFacts.green_job_names" in fetch_body
    assert "green_jobs=green_jobs" in fetch_body


def test_the_timed_alarm_workflows_are_watched_and_confirm_on_first_sight() -> None:
    module = ast.parse(_ENTRY.read_text())
    constants = {
        target.id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    assert constants["_MODEL_PRICING"] == "update-model-pricing.yml"
    assert constants["_BRANCH_PROTECTION"] == "audit-branch-protection.yml"
    assert constants["_RANDOM_ORDER"] == "random-order.yml"

    def assigned(name: str) -> str:
        return ast.unparse(
            next(
                node.value
                for node in module.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            )
        )

    alarms = assigned("_ALARM_WORKFLOWS")
    full_suite = assigned("_FULL_SUITE_WORKFLOWS")
    for name in ("_SCHEDULED_AUDITS", "_MODEL_PRICING", "_BRANCH_PROTECTION", "_RANDOM_ORDER"):
        assert name in alarms
        assert name not in full_suite

    watch_call = next(
        node
        for node in ast.walk(_function(_ENTRY, "watch_round"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "watch_once"
    )
    workflows_kw = ast.unparse(
        next(kw.value for kw in watch_call.keywords if kw.arg == "workflows")
    )
    liveness_kw = ast.unparse(next(kw.value for kw in watch_call.keywords if kw.arg == "liveness"))
    for name in ("_MODEL_PRICING", "_BRANCH_PROTECTION", "_RANDOM_ORDER"):
        assert name in workflows_kw
    assert "LivenessStream('model-pricing', _MODEL_PRICING, NIGHTLY_STALE_AFTER)" in liveness_kw
    assert "LivenessStream('random-order', _RANDOM_ORDER, NIGHTLY_STALE_AFTER)" in liveness_kw
    assert (
        "LivenessStream('branch-protection-audit', _BRANCH_PROTECTION, WEEKLY_STALE_AFTER)"
        in liveness_kw
    )

    fetch_body = ast.unparse(_function(_ENTRY, "fetch_runs"))
    assert "workflow in _ALARM_WORKFLOWS and failure.kind == FAILURE_TEST" in fetch_body


# meiva:note
# YAML 1.1 reads a workflow's `on:` key as the boolean True, so the triggers are looked up by it.
def test_every_workflow_with_a_schedule_trigger_is_read_by_the_watch() -> None:
    import yaml

    module = ast.parse(_ENTRY.read_text())
    constants = {
        target.id: node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    watch_call = next(
        node
        for node in ast.walk(_function(_ENTRY, "watch_round"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "watch_once"
    )
    workflows_kw = next(kw.value for kw in watch_call.keywords if kw.arg == "workflows")
    watched = {
        constants[element.id]
        for element in ast.walk(workflows_kw)
        if isinstance(element, ast.Name) and element.id in constants
    }
    timed = set()
    for path in sorted((_ENTRY.parents[3] / ".github" / "workflows").glob("*.yml")):
        triggers = yaml.safe_load(path.read_text()).get(True)
        if isinstance(triggers, dict) and "schedule" in triggers:
            timed.add(path.name)

    assert timed - watched == set(), (
        "a workflow with a `schedule:` trigger that main-red-watch does not read is a red nobody "
        f"sees: {sorted(timed - watched)}"
    )
