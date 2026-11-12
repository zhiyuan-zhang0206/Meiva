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
