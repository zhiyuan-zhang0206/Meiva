from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script


@pytest.mark.parametrize("directory_already_listed", [False, True])
def test_load_skill_script_imports_sibling_without_leaking_path(
    tmp_path: Path, directory_already_listed: bool
) -> None:
    sibling = "skill_script_test_sibling.py"
    _ = (tmp_path / sibling).write_text("VALUE = 42\n", encoding="utf-8")
    script = tmp_path / "entry.py"
    _ = script.write_text("from skill_script_test_sibling import VALUE\n", encoding="utf-8")
    original_path = sys.path

    saved = original_path[:]
    try:
        if directory_already_listed:
            shadow = tmp_path / "shadow"
            shadow.mkdir()
            _ = (shadow / sibling).write_text("VALUE = 0\n", encoding="utf-8")
            original_path.extend([str(shadow), str(tmp_path), str(tmp_path)])
        before_load = original_path[:]

        module = load_skill_script(str(script))
        assert vars(module)["VALUE"] == 42
        assert sys.path is original_path
        assert original_path == before_load
    finally:
        original_path[:] = saved
        _ = sys.modules.pop("entry_under_test", None)
        _ = sys.modules.pop("skill_script_test_sibling", None)


def test_load_skill_script_restores_path_on_failure(tmp_path: Path) -> None:
    script = tmp_path / "broken.py"
    _ = script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "raise RuntimeError('import failed')\n",
        encoding="utf-8",
    )
    original_path = sys.path
    saved = original_path[:]

    try:
        with pytest.raises(RuntimeError, match="import failed"):
            _ = load_skill_script(str(script))
        assert sys.path is original_path
        assert original_path == saved
    finally:
        original_path[:] = saved
        _ = sys.modules.pop("broken_under_test", None)


def test_load_skill_script_restores_both_paths_after_rebinding(tmp_path: Path) -> None:
    script = tmp_path / "rebind.py"
    _ = script.write_text(
        "import sys\n"
        "class ScriptError(ValueError):\n    pass\n"
        "failure = ScriptError('import failed after rebinding')\n"
        "sys.path = []\n"
        "raise failure\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "meiva_builtins.skills.tests.test_skill_script", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _check_rebinding_in_child(script: str) -> None:
    original_path = sys.path
    saved = original_path[:]
    with pytest.raises(ValueError, match=r"^import failed after rebinding$") as caught:
        _ = load_skill_script(script)
    assert caught.value is vars(sys.modules["rebind_under_test"])["failure"]
    assert original_path == saved
    assert sys.path == saved


if __name__ == "__main__":
    _check_rebinding_in_child(sys.argv[1])
