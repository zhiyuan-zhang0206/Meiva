from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from base.host.boundary.json_values import is_object_list
from scripts.audit import input_closures
from scripts.ci import test_selector

_FILES = {
    "pyproject.toml": (
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests", "base/**/tests", "cli/**/tests", "scripts/**/tests"]\n'
    ),
    "conftest.py": 'pytest_plugins = ["tests.fixtures.env"]\n',
    "uv.lock": "",
    "tests/fixtures/env.py": "",
    "cli/__init__.py": "",
    "cli/plugins/__init__.py": "",
    "cli/plugins/guard.py": "",
    "tests/unit/test_a.py": "from base.lm import engine\n\ndef test_a(): pass\n",
    "tests/unit/test_b.py": "def test_b(): pass\n",
    "tests/sub/conftest.py": "",
    "tests/sub/test_c.py": "def test_c(): pass\n",
    "tests/test_lint_scan.py": "def test_scan(): pass\n",
    "base/lm/__init__.py": "",
    "base/lm/engine.py": "",
    "base/lm/sibling.py": "",
    "tests/unit/test_sibling.py": "from base.lm import sibling\n\ndef test_sibling(): pass\n",
    "meizel.toml": "",
    "meizel.policy.toml": "",
    "scripts/tests/test_reads_manifest.py": 'MANIFEST = "meizel.toml"\n',
    "base/lm/schema.json": "{}\n",
    "base/lm/tests/test_lm.py": "from base.lm import engine\n\ndef test_lm(): pass\n",
    "base/lm/tests/helper.py": "",
    "base/agents/runner.py": "",
    "ops/worker.py": "",
    "scripts/tests/test_reads_github.py": 'WORKFLOW = ".github/workflows/ci.yml"\n',
    "scripts/tests/test_reads_agents.py": 'SKILLS = root / ".agents" / "skills"\n',
    "scripts/tests/test_reads_skill.py": 'NAME = "SKILL.md"\n',
    "scripts/tests/test_reads_data.py": (
        "from pathlib import Path\n\ndef test_data(path): Path(path).read_text()\n"
    ),
    "scripts/tests/test_unbounded.py": "import importlib\n\nimportlib.import_module(name)\n",
    "ui/web/src/App.tsx": "",
    ".github/workflows/ci.yml": "",
    ".github/workflows/other.yml": "",
    ".agents/skills/demo/SKILL.md": "",
    ".pre-commit-config.yaml": "",
    "docs/guide.md": "",
    "README.md": "",
    "LICENSE": "",
}


def _files(entry: dict[str, object]) -> list[str]:
    files = entry["files"]
    assert is_object_list(files)
    return [item for item in files if isinstance(item, str)]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for relative, content in _FILES.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)
    return tmp_path


def test_a_file_is_in_a_closure_exactly_when_changing_it_selects_the_test(repo: Path) -> None:
    found = input_closures.closures(repo, _FILES)
    unbounded = {test for test, entry in found.items() if "unbounded" in entry}
    assert unbounded >= {"scripts/tests/test_unbounded.py", "tests/test_lint_scan.py"}
    for path in sorted(_FILES):
        selection = test_selector.select_tests([path], repo_root=repo)
        everything = set(found)
        selected = everything if selection.decision == "FULL" else set(selection.tests)
        inverse = {
            test for test, entry in found.items() if "files" in entry and path in _files(entry)
        }
        assert selected - unbounded == inverse, path


def test_a_data_file_reaches_the_tests_that_may_read_it_unseen(repo: Path) -> None:
    entry = input_closures.closures(repo, _FILES)["scripts/tests/test_reads_data.py"]
    assert "files" in entry
    assert "base/lm/schema.json" in _files(entry)
    assert "ops/worker.py" not in _files(entry)
    other = input_closures.closures(repo, _FILES)["tests/unit/test_b.py"]
    assert "base/lm/schema.json" not in _files(other)


def test_a_closure_holds_its_globals_and_its_own_file(repo: Path) -> None:
    entry = input_closures.closures(repo, _FILES)["tests/unit/test_b.py"]
    files = set(_files(entry))
    assert {"tests/unit/test_b.py", "uv.lock", "conftest.py", "pyproject.toml"} <= files
    assert "base/lm/engine.py" not in files


def test_a_change_of_several_files_selects_no_more_than_the_union_of_its_files(
    repo: Path,
) -> None:
    found = input_closures.closures(repo, _FILES)
    unbounded = {test for test, entry in found.items() if "unbounded" in entry}
    paths = sorted(_FILES)
    for first, second in itertools.combinations(paths, 2):
        selection = test_selector.select_tests([first, second], repo_root=repo)
        if selection.decision == "FULL":
            continue
        union = {
            test
            for test, entry in found.items()
            if "files" in entry and ({first, second} & set(_files(entry)))
        }
        assert set(selection.tests) - unbounded <= union, (first, second)


def test_a_deleted_file_leaves_the_closure_it_was_in(repo: Path) -> None:
    before = input_closures.closures(repo, _FILES)
    assert "base/lm/engine.py" in _files(before["tests/unit/test_a.py"])
    (repo / "base/lm/engine.py").unlink()
    remaining = [path for path in _FILES if path != "base/lm/engine.py"]
    after = input_closures.closures(repo, remaining)
    assert "base/lm/engine.py" not in _files(after["tests/unit/test_a.py"])
