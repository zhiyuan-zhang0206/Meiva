from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.telemetry import EventPipeline
from cli.commands.management import schedule_verify_checks
from cli.commands.management import schedules_verify as _verify
from cli.commands.management.schedules_verify import CheckFile, RowsFromFile
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

_NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


_OLD_HIERARCHY = """\
import time
from datetime import UTC, datetime

from schedules.jobs.catchup import cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire

CRON = "* * * * *"


def _fire(_trigger):
    pass


def main():
    nxt = next_fire(CRON, after=datetime.now(UTC), timezone=cluster_timezone())
    fire_slot_once(nxt, None, fire=_fire)
"""

_NEW_HIERARCHY = (
    _OLD_HIERARCHY.replace("def _fire(_trigger):", "def _fire(_slot, _trigger):")
    .replace(
        "fire_slot_once(nxt, None, fire=_fire)",
        "fire_slot_once(db, nxt, None, fire=_fire, schedule_id=1)",
    )
    .replace("def main():\n", "def main():\n    db = None\n")
)


def _check(script: str) -> str | None:
    return _verify.check_script(script)


def test_an_old_signature_call_is_red_where_the_import_alone_was_green() -> None:
    red = _check(_OLD_HIERARCHY)
    assert red is not None and red.startswith("call-signature:")
    assert "fire_slot_once(): missing a required argument: 'payload'" in red


def test_the_current_signature_is_green() -> None:
    assert _check(_NEW_HIERARCHY) is None


def test_the_attribute_form_and_an_alias_are_resolved() -> None:
    script = (
        "import schedules.jobs.catchup as c\nimport schedules.jobs.catchup\n"
        "c.fire_slot_once(1, fire=print)\n"
        "schedules.jobs.catchup.fire_slot_once(1, 2, fire=print)\n"
    )
    red = _check(script)
    assert red is not None
    assert "L3 c.fire_slot_once()" in red and "L4 schedules.jobs.catchup.fire_slot_once()" in red


def test_an_unknown_keyword_is_red() -> None:
    red = _check(
        "from schedules.jobs.catchup import fire_slot_once\nfire_slot_once(1, 2, 3, fire=print, schedule_id=1, nope=1)\n"
    )
    assert red is not None and "unexpected keyword argument 'nope'" in red


def test_a_removed_module_attribute_is_red() -> None:
    red = _check("import schedules.jobs.catchup\nschedules.jobs.catchup.gone_function(1)\n")
    assert red is not None and "schedules.jobs.catchup has no `gone_function`" in red


@pytest.mark.parametrize(
    "script",
    [
        "from schedules.jobs.catchup import fire_slot_once\n\na, k = (), {}\nfire_slot_once(*a, **k)\n",
        "from schedules.jobs.catchup import fire_slot_once\n\ndef fire_slot_once(): ...\n\nfire_slot_once()\n",
        "from schedules.jobs.catchup import fire_slot_once\n\ndef f(fire_slot_once):\n    fire_slot_once()\n",
        "from base.db import Database\nfrom base.db.code_version_gate import ProcessDbGate\n"
        "def connect(gate: ProcessDbGate):\n"
        "    Database.from_settings(gate=gate).connect()\n",
        "import meiva\nmeiva.agents.spawn(prompt='x', label='lead')\n",
        "import meiva\nresults = meiva.tasks.get('x').results\n",
        "import meiva\n\ndef scan(task_id):\n    return meiva.tasks.get(task_id).results\n",
        "import json\njson.dumps()\n",
        "import os\nos.getpid(1)\n",
    ],
)
def test_calls_it_cannot_prove_wrong_are_not_red(script: str) -> None:
    assert _check(script) is None


def test_a_class_constructor_is_checked_too() -> None:
    red = _check("from base.db import Database\nDatabase(1, 2, 3, 4, 5, 6)\n")
    assert red is not None and "Database()" in red


def test_check_file_prints_the_call_signature_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text(_OLD_HIERARCHY, encoding="utf-8")
    assert (
        _verify.cmd_schedules_verify(
            source=CheckFile(str(stale)), notify=True, now=_NOW, producer=operator_pipeline
        )
        == 1
    )
    assert "CHECK-RED missing=call-signature:L16 fire_slot_once()" in capsys.readouterr().out


def test_the_sweep_checks_agent_written_rows_like_built_in_ones(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    rows = tmp_path / "rows.json"
    rows.write_text(
        json.dumps([[1, "built-in", _NEW_HIERARCHY], [9, "agent-made-poller", _OLD_HIERARCHY]])
    )
    assert (
        _verify.cmd_schedules_verify(
            source=RowsFromFile(str(rows)), notify=False, now=_NOW, producer=operator_pipeline
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "checked=2 green=1 red=1 rc=1" in out
    assert "RED id=9 name=agent-made-poller missing=call-signature:" in out


def test_every_shipped_built_in_script_binds_against_the_library() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[4] / "schedules" / "manifest.json").read_text()
    )
    reds = {}
    for entry in manifest["builtin_schedules"]:
        script = (Path(__file__).resolve().parents[4] / "schedules" / entry["script"]).read_text()
        if red := _check(script):
            reds[entry["name"]] = red
    assert reds == {}


def test_a_read_of_a_name_nothing_imported_is_red() -> None:
    red = _check("from base.config import settings\nroot = base.__file__\n")
    assert red is not None and red.startswith("undefined-name:")
    assert "L2 base" in red


def test_a_read_inside_a_function_is_red_too() -> None:
    red = _check("from base.config import settings\n\n\ndef run():\n    return base.__file__\n")
    assert red is not None and red.startswith("undefined-name:") and "L5 base" in red


def test_names_the_script_binds_or_the_language_provides_are_green() -> None:
    assert (
        _check(
            "import base.config\nfrom typing import Any\n\n"
            "if __name__ == '__main__':\n"
            "    value: Any = None\n"
            "    items = [v for v in range(3)]\n"
            "    print(__file__, __name__, value, items, base.config.__name__)\n"
        )
        is None
    )


def test_annotations_are_not_read() -> None:
    assert _check("def f(value: zz_missing_type) -> zz_missing_return:\n    return value\n") is None


@pytest.mark.parametrize(
    "script",
    [
        "def identity[T](value):\n    return T\n",
        "def wrap[**P](fn):\n    return P\n",
        "def pack[*Ts](values):\n    return Ts\n",
        "class Bag[T]:\n    item = T\n",
        "class BagP[**P]:\n    item = P\n",
        "class BagT[*Ts]:\n    item = Ts\n",
        "type Bag[T] = list[T]\n",
        "type BagP[**P] = list[P]\n",
        "type BagTs[*Ts] = list[*Ts]\n",
    ],
    ids=[
        "fn-typevar",
        "fn-paramspec",
        "fn-typevartuple",
        "class-typevar",
        "class-paramspec",
        "class-typevartuple",
        "alias-typevar",
        "alias-paramspec",
        "alias-typevartuple",
    ],
)
def test_pep695_type_parameters_are_bindings(script: str) -> None:
    assert _check(script) is None


@pytest.mark.parametrize(
    "script",
    [
        "def f[T: zz_missing_bound]():\n    return T\n",
        "class C[T: zz_missing_bound]:\n    pass\n",
        "type A[T: zz_missing_bound] = list[T]\n",
    ],
    ids=["fn", "class", "alias"],
)
def test_type_parameter_bounds_are_not_read(script: str) -> None:
    assert _check(script) is None


def test_a_missing_name_next_to_a_bound_type_param_is_still_red() -> None:
    red = _check("def identity[T](value: T) -> T:\n    return zz_missing_value\n")
    assert red is not None and red.startswith("undefined-name:")
    assert "L2 zz_missing_value" in red


def test_a_missing_name_in_a_type_alias_value_is_still_red() -> None:
    red = _check("type Bag[T] = list[zz_missing_alias]\n")
    assert red is not None and red.startswith("undefined-name:") and "L1 zz_missing_alias" in red


def test_a_conditional_import_binding_is_green() -> None:
    assert (
        _check(
            "try:\n    import zz_maybe\nexcept ImportError:\n    zz_maybe = None\nprint(zz_maybe)\n"
        )
        is None
    )


def test_a_star_import_disables_the_check() -> None:
    assert _check("from os.path import *\nprint(zz_whatever_from_the_star)\n") is None


def test_check_file_prints_the_undefined_name_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text("from base.config import settings\nroot = base.__file__\n", encoding="utf-8")
    assert (
        _verify.cmd_schedules_verify(
            source=CheckFile(str(stale)), notify=True, now=_NOW, producer=operator_pipeline
        )
        == 1
    )
    assert "CHECK-RED missing=undefined-name:L2 base" in capsys.readouterr().out


# meiva:note
# The seam the split introduced: the process entry decides which callees are repository code, the
# check binds only what it says yes to. Pinned here because the entry's own end-to-end path cannot
# see the difference -- a predicate that answers `False` to everything reports no problems at all,
# and every call-site case above would go green without saying why.
def test_the_repository_predicate_decides_which_call_sites_are_bound() -> None:
    import ast
    import json

    tree = ast.parse("import json\n\njson.dumps(1, 2, 3)\n")
    nothing_is_repo = schedule_verify_checks.call_site_problems(
        tree, {"json": json}, is_repo=lambda _target: False
    )
    everything_is_repo = schedule_verify_checks.call_site_problems(
        tree, {"json": json}, is_repo=lambda _target: True
    )
    assert nothing_is_repo == []
    assert len(everything_is_repo) == 1 and "too many positional arguments" in everything_is_repo[0]


# meiva:note
# The name check is decided in the strict module too, and a `none` declaration on the entry must not
# leave that module exercised only through a subprocess: the two cases below call it directly.
def test_the_strict_module_reports_a_name_nothing_binds() -> None:
    import ast

    tree = ast.parse("root = base.__file__\n")
    assert schedule_verify_checks.undefined_name_problems(tree) == ["L1 base"]


def test_the_strict_module_keeps_a_bound_name_green() -> None:
    import ast

    tree = ast.parse(
        "from pathlib import Path\n\nroot = Path(base.__file__)\n".replace("base", "Path")
    )
    assert schedule_verify_checks.undefined_name_problems(tree) == []


# meiva:note
# What counts as repository code is a rule of the strict module, not of the entry that reads the
# filesystem: the entry keeps only "where is this callee defined" and the `resolve` that normalizes
# it. Both wrong answers are silent -- a wrong `True` binds a signature that is not the one the
# script calls, a wrong `False` skips a callee the check must bind -- so each reason to answer
# either way is pinned here.
def test_the_strict_module_decides_what_counts_as_repository_code(tmp_path: Path) -> None:
    root = tmp_path / "root"
    is_repo = schedule_verify_checks.is_repo_source
    # meiva:note
    # One line per rule the predicate carries, and each message names what the rule protects so a
    # red line reports the decision that broke instead of `False is not True`. Both wrong answers
    # are silent: a false True binds a signature that is not the one the script will call, a false
    # False stops binding a callee the guard exists to check.
    assert is_repo(root / "schedules/jobs/catchup.py", root=root) is True, (
        "a file under the checkout is repository code; answering False drops the signature check "
        "for every call a schedule script makes into the repo, which is the drift this guard exists "
        "to catch (the 2026-10-03 class)"
    )
    assert is_repo(root / ".venv/lib/python3.12/site-packages/base/clock.py", root=root) is False, (
        "the installed copy in the checkout's virtualenv is not repository code; answering True "
        "binds the script's calls against a stale installed module instead of the source, turning "
        "an edit under review into a red schedule script for the wrong reason"
    )
    assert is_repo(root / "services/app/site-packages/vendor/mod.py", root=root) is False, (
        "a vendored site-packages tree inside the checkout is still not ours; answering True binds "
        "third-party signatures and turns their upgrades into red schedule scripts"
    )
    assert is_repo(root / "meiva/sdk_surface/settings.py", root=root) is False, (
        "the agent SDK package is skipped on purpose: plugins install its namespaces and wrap its "
        "functions at load time, so the static module is neither the name set nor the call "
        "contract; answering True red-flags valid SDK calls"
    )
    assert is_repo(tmp_path / "elsewhere/schedules/jobs/catchup.py", root=root) is False, (
        "a file outside the checkout is not ours; answering True makes the verdict depend on what "
        "else the machine can import, so one script passes on one host and fails on another"
    )
    assert is_repo(None, root=root) is False, (
        "an unknowable source is never guessed ours; answering True invents a signature check for a "
        "callee nobody could read, and the author cannot act on the result"
    )
