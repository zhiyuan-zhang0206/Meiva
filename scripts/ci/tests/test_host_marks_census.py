# meiva:note
# The census script is the runtime half of the host-lane gate (task #5531): the registry says
# a lane selects `-m requires_host`, this records what that selection collected on the host
# that ran it. Its two failure modes are worth separate cases - a parser that keeps summary
# lines would report a denominator nobody can open, and an empty selection must be refused
# rather than written as a census of zero.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci import host_marks_census as census

_COLLECT_OUTPUT = """base/native_process/tests/test_proc_tree.py::test_a[1.0]
base/native_process/tests/test_proc_tree.py::test_a[60.0]
services/desktop/permissions_helper/tests/test_grant_prompt_once.py::test_b
19/26139 tests collected (26120 deselected) in 15.38s
"""


def test_only_node_ids_are_kept() -> None:
    assert census.node_ids(_COLLECT_OUTPUT) == [
        "base/native_process/tests/test_proc_tree.py::test_a[1.0]",
        "base/native_process/tests/test_proc_tree.py::test_a[60.0]",
        "services/desktop/permissions_helper/tests/test_grant_prompt_once.py::test_b",
    ]


def test_an_empty_selection_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        census.record([], tmp_path / "census.json", "darwin")
    assert "collected nothing" in str(refused.value)
    assert not (tmp_path / "census.json").exists()


def test_the_census_names_the_selection_and_sorts_its_node_ids(tmp_path: Path) -> None:
    path = tmp_path / "census.json"
    document = census.record(["b.py::test_z", "a.py::test_y"], path, "darwin")
    assert document == {
        "host": "darwin",
        "selection": "-m requires_host",
        "collected": 2,
        "node_ids": ["a.py::test_y", "b.py::test_z"],
    }
    assert json.loads(path.read_text(encoding="utf-8")) == document
