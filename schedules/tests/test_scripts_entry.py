# meiva:note
# A built-in schedule script as the runner runs it.
#
# The script is stored in the database and materialized into the schedule's own directory, away
# from the checkout, so its imports must resolve from the installed module roots alone
# (`meiva schedules verify` checks the same for the stored copies). Run as `__main__` with a
# schedule id and a database, each script assembles its schedule and reaches its loop: a child
# process that is still waiting a few seconds after it started has got past every line of
# assembly, which is where a wrong keyword or a misspelled name would show.

from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path

from base.host.proc import run_bounded

_SCHEDULES = Path(__file__).resolve().parents[1]
_CHILD = Path(__file__).parent / "children" / "run_scripts.py"
# meiva:note
# Every built-in entry, including the ones that live beside the job they compose instead of in the
# registry root: `schedules/manifest.json` names a relative path, so an entry script may sit in a
# subpackage. The pattern is the manifest's own suffix, not "any .py under schedules/".
_SCRIPTS = sorted(_SCHEDULES.glob("**/*-schedule.py"))


# meiva:note
# The negative half: a module in the same subpackage that is not an entry must stay out of the
# collected set. Without it the recursive pattern would quietly turn any new helper into an entry
# the day someone names one `...-schedule.py` by accident.
def test_a_subpackage_module_that_is_not_an_entry_is_not_collected() -> None:
    collected = {path.name for path in _SCRIPTS}
    helper = _SCHEDULES / "jobs" / "watch" / "main_red_watch.py"

    assert helper.is_file()
    assert helper.name not in collected
    assert "main-red-watch-schedule.py" in collected


def test_every_builtin_script_imports_from_a_materialized_copy(tmp_path: Path) -> None:
    copies: list[Path] = []
    for index, script in enumerate(_SCRIPTS, start=11):
        directory = tmp_path / "schedules" / str(index)
        directory.mkdir(parents=True)
        copies.append(Path(shutil.copy(script, directory / script.name)))
    manifest = json.loads((_SCHEDULES / "manifest.json").read_text())["builtin_schedules"]
    assert {script.relative_to(_SCHEDULES).as_posix() for script in _SCRIPTS} == {
        entry["script"] for entry in manifest
    }

    child = run_bounded(
        [sys.executable, "-I", str(_CHILD), "imports", *map(str, copies)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert child.returncode == 0, child.stderr
    outcome = json.loads(child.stdout)
    assert {Path(path).name: result for path, result in outcome.items()} == {
        copy.name: "ok" for copy in copies
    }


def test_every_builtin_entry_is_single_round() -> None:
    resident_drivers = {"run_loop", "run_weekly", "run_watch", "run_keepalive"}
    violations: dict[str, list[str]] = {}
    for script in _SCRIPTS:
        tree = ast.parse(script.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in resident_drivers
            ):
                found.append(node.func.id)
            if isinstance(node, ast.ImportFrom):
                found.extend(alias.name for alias in node.names if alias.name in resident_drivers)
        if found:
            violations[str(script.relative_to(_SCHEDULES))] = sorted(set(found))

    assert violations == {}
