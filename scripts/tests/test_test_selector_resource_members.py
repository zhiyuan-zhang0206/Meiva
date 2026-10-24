from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_impact, test_selector

_READER = "tests/consumer/test_reader.py"
_PLUGIN = "meiva_builtins/plugins/one"
_SOURCE = """from pathlib import Path

def discover():
    paths = []
    local = Path(__file__).resolve().parents[1] / "meiva_builtins" / "plugins"
    for root in (local,):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.name.startswith("."):
                continue
            member = entry / ".mcp.json"
            if member.is_file():
                paths.append(member)
    return paths
"""


def _write(root: Path, path: str, text: str = "") -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


# meiva:note
# Git commands operate only on the repository created by this test.
def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(root: Path, source: str = _SOURCE) -> Path:
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "meiva_builtins/**/tests"]\n',
    )
    _write(root, "base/discovery.py", source)
    _write(root, _READER, "from base import discovery\n")
    _write(root, f"{_PLUGIN}/tests/test_unrelated.py", "def test_unrelated(): pass\n")
    return root


@pytest.mark.parametrize("change", ["add", "delete", "rename-out", "rename-in", "rename-plugin"])
def test_membership_changes_query_both_base_and_head(tmp_path: Path, change: str) -> None:
    root = _repo(tmp_path)
    before = f"{_PLUGIN}/.mcp.json"
    after = f"{_PLUGIN}/ignored.json"
    if change == "rename-in":
        before, after = after, before
    if change == "rename-plugin":
        after = "meiva_builtins/plugins/two/.mcp.json"
    if change != "add":
        _write(root, before, "{}")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    if change == "add":
        _write(root, before, "{}")
        changed = [before]
        # meiva:note
        # The new reader makes this assertion depend on the head graph.
        _write(root, "tests/consumer/test_new.py", "from base import discovery\n")
    elif change == "delete":
        (root / before).unlink()
        changed = [before]
        # meiva:note
        # Removing discovery makes this assertion depend on the base graph.
        _write(root, "base/discovery.py")
    else:
        (root / after).parent.mkdir(parents=True, exist_ok=True)
        (root / before).rename(root / after)
        changed = [before, after]
    result = test_selector.select_tests(changed, repo_root=root, base_ref=base)
    assert result.decision == "SELECTED", result.as_json()
    assert _READER in result.tests
    if change == "add":
        assert "tests/consumer/test_new.py" in result.tests
    assert result.diagnostics == ()


@pytest.mark.parametrize("suffix", ["tests/.mcp.json", "tests/test_unrelated.py", "ignored.json"])
def test_nonmembers_do_not_reach_the_reader(tmp_path: Path, suffix: str) -> None:
    root = _repo(tmp_path)
    path = f"{_PLUGIN}/{suffix}"
    _write(root, path)
    result = test_selector.select_tests([path], repo_root=root)
    assert result.decision == "SELECTED", result.as_json()
    assert _READER not in result.tests
    assert result.diagnostics == ()


def test_external_alternative_remains_opaque_for_data_changes(tmp_path: Path) -> None:
    root = _repo(tmp_path, _SOURCE.replace("(local,)", "(local, external_root())"))
    path = f"{_PLUGIN}/ignored.json"
    _write(root, path)
    data = test_selector.select_tests([path], repo_root=root)
    python = test_selector.select_tests([f"{_PLUGIN}/tests/test_unrelated.py"], repo_root=root)
    assert _READER in data.tests
    assert any("external_root()" in item for item in data.diagnostics)
    assert _READER not in python.tests
    assert python.diagnostics == ()


@pytest.mark.parametrize("escape", ["sink(local)\n    ", "local = other\n    "])
def test_failed_proof_keeps_ordinary_subtree_consumers(tmp_path: Path, escape: str) -> None:
    root = _repo(
        tmp_path, _SOURCE.replace("for root in (local,):", escape + "for root in (local,):")
    )
    path = f"{_PLUGIN}/tests/test_unrelated.py"
    assert _READER in test_selector.select_tests([path], repo_root=root).tests


def test_ordinary_and_member_edges_share_transitive_consumers(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "tests/consumer/test_indirect.py", "from tests.consumer import test_reader\n")
    tests = test_selector.load_checkout(root).collectable
    impact = test_impact.build_impact(root, tests)
    expected = {_READER, "tests/consumer/test_indirect.py"}
    assert impact.runtime_tests(f"{_PLUGIN}/.mcp.json") == expected
    assert impact.runtime_tests("base/discovery.py") == expected
    assert impact.runtime_tests(f"{_PLUGIN}/tests/test_unrelated.py") == {
        f"{_PLUGIN}/tests/test_unrelated.py"
    }


def test_failed_proof_external_input_still_selects_data_consumers(tmp_path: Path) -> None:
    source = _SOURCE.replace("(local,)", "(local, external_root())")
    source = source.replace(
        "def discover():",
        "ROOT = Path(__file__).resolve().parents[1]\nsink(ROOT)\n\ndef discover():",
    ).replace('Path(__file__).resolve().parents[1] / "meiva_builtins"', 'ROOT / "meiva_builtins"')
    root = _repo(tmp_path, source)
    path = "base/other/one/config.json"
    _write(root, path, "{}")
    result = test_selector.select_tests([path], repo_root=root)
    assert result.decision == "SELECTED", result.as_json()
    assert _READER in result.tests
    assert any("external_root()" in item for item in result.diagnostics)
