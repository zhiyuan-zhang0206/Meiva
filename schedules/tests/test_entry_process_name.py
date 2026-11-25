from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

from base.host.proc import run_bounded

_CHILD = Path(__file__).parent / "children" / "entry_process_child.py"
_SCHEDULES = Path(__file__).resolve().parents[1]
_ROOT = _SCHEDULES.parent
_LAUNCHED = {"MEIVA_SCHEDULE_ID": "5", "MEIVA_AGENT_ID": "405"}


def _run(tmp_path: Path, *process: str, launched: bool) -> dict[str, object]:
    home = tmp_path / "home"
    home.mkdir()
    child = run_bounded(
        [sys.executable, str(_CHILD), str(home), *process],
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")},
            "PYTHONPATH": str(_ROOT),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            **(_LAUNCHED if launched else {}),
        },
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    result: dict[str, object] = json.loads(child.stdout)
    return result


def test_characterization_an_unnamed_schedule_is_labelled_sdk_and_drops_its_metrics(
    tmp_path: Path,
) -> None:
    assert _run(tmp_path, launched=False) == {
        "at_import": "unknown",
        "before_sdk": "unknown",
        "after_sdk": "sdk",
        "exported": False,
    }


def test_a_named_schedule_keeps_its_process_across_the_first_sdk_call(tmp_path: Path) -> None:
    assert _run(tmp_path, "main-red-watch", launched=False) == {
        "at_import": "unknown",
        "before_sdk": "main-red-watch",
        "after_sdk": "main-red-watch",
        "exported": True,
    }


def test_a_launched_schedule_replaces_the_sdk_label_its_import_bound(tmp_path: Path) -> None:
    assert _run(tmp_path, "main-red-watch", launched=True) == {
        "at_import": "sdk",
        "before_sdk": "main-red-watch",
        "after_sdk": "main-red-watch",
        "exported": True,
    }


def test_characterization_an_unnamed_launched_schedule_stays_sdk(tmp_path: Path) -> None:
    assert _run(tmp_path, launched=True) == {
        "at_import": "sdk",
        "before_sdk": "sdk",
        "after_sdk": "sdk",
        "exported": False,
    }


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _module_file(module: str) -> Path | None:
    candidate = _ROOT.joinpath(*module.split(".")).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _local_imports(tree: ast.Module) -> list[tuple[Path, ast.ImportFrom]]:
    return [
        (target, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("schedules.")
        and (target := _module_file(node.module)) is not None
    ]


def _closure(script: Path) -> list[Path]:
    seen: list[Path] = [script]
    pending = [script]
    while pending:
        for target, _node in _local_imports(_tree(pending.pop())):
            if target not in seen:
                seen.append(target)
                pending.append(target)
    return seen


def _constant(path: Path, name: str) -> str | None:
    tree = _tree(path)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and [t.id for t in node.targets if isinstance(t, ast.Name)] == [name]
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    for target, node in _local_imports(tree):
        for alias in node.names:
            if (alias.asname or alias.name) == name:
                return _constant(target, alias.name)
    return None


def _process_values(path: Path, function: str) -> list[str | None]:
    values: list[str | None] = []
    for call in ast.walk(_tree(path)):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == function
        ):
            for keyword in call.keywords:
                if keyword.arg == "process":
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        values.append(value.value)
                    elif isinstance(value, ast.Name):
                        values.append(_constant(path, value.id))
                    else:
                        values.append(None)
    return values


def _emitting_scripts() -> list[Path]:
    scripts = sorted(
        script
        for script in _SCHEDULES.rglob("*-schedule.py")
        if "tests" not in script.relative_to(_SCHEDULES).parts
    )
    return [
        script
        for script in scripts
        if any(_calls(module, "emit_system_event") for module in _closure(script))
    ]


def _calls(path: Path, function: str) -> bool:
    return any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == function
        for call in ast.walk(_tree(path))
    )


def test_the_emitting_schedules_are_discovered_not_listed() -> None:
    assert {script.name for script in _emitting_scripts()} >= {
        "agent-watch-schedule.py",
        "c9-daily-report-schedule.py",
        "claude-auth-keepalive-schedule.py",
        "debt-sweep-daily-schedule.py",
        "harness-cli-update-schedule.py",
        "main-red-watch-schedule.py",
    }


def test_every_schedule_that_emits_a_system_event_names_that_process_at_entry() -> None:
    for script in _emitting_scripts():
        [entry] = _process_values(script, "open_schedule_entry")
        assert entry, f"{script.name}: open_schedule_entry's process is not a string constant"
        emitted = {
            value
            for module in _closure(script)
            for value in _process_values(module, "emit_system_event")
            if value is not None
        }
        assert emitted <= {entry}, f"{script.name}: entry names {entry!r}, emitters {emitted}"
