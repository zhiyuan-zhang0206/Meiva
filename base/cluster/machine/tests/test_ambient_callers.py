from __future__ import annotations

import ast
import subprocess
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.registry
_REPO = Path(__file__).resolve().parents[4]
_AMBIENT = "base/cluster/machine/ambient.py"

CALLERS: dict[str, tuple[str, int]] = {
    "meiva_builtins/skills/platform/meiva-self-evolution/scripts/collect.py": ("O", 1),
    "meiva_builtins/plugins/meiva_memory/setup.py": ("B2", 1),
    "ops/cluster/rpc.py": ("O", 1),
    "scripts/entrypoints/agent.py": ("O", 1),
    "services/agent_runner/agent_ops/boot.py": ("S", 1),
}
_BATCHES = {"B2", "S", "O"}


def _is_test(path: str) -> bool:
    return "/tests/" in path or path.startswith("tests/") or Path(path).name.startswith("test_")


def _found() -> dict[str, Counter[str]]:
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    found: dict[str, Counter[str]] = {}
    for path in tracked:
        if _is_test(path) or path == _AMBIENT:
            continue
        source = (_REPO / path).read_text()
        if "gateway_access_from_settings" not in source:
            continue
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "gateway_access_from_settings":
                continue
            line = lines[node.lineno - 1]
            label = line.split("# ambient:", 1)[1].split()[0] if "# ambient:" in line else "?"
            found.setdefault(path, Counter())[label] += 1
    return found


def test_every_call_names_the_batch_that_removes_it() -> None:
    unlabelled = {path: dict(labels) for path, labels in _found().items() if set(labels) - _BATCHES}
    assert unlabelled == {}


def test_the_callers_are_exactly_the_recorded_list() -> None:
    found = {path: (next(iter(labels)), sum(labels.values())) for path, labels in _found().items()}
    assert all(len(labels) == 1 for labels in _found().values()), "one batch per file"
    added = {path: value for path, value in found.items() if value != CALLERS.get(path)}
    removed = sorted(path for path in CALLERS if path not in found)
    assert added == {} and removed == [], (
        "new code takes a GatewayAccess from its composition root; "
        "a caller that moved to a root leaves CALLERS in the same change"
    )
