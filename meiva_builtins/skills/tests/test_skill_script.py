from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script


def test_load_skill_script_imports_sibling_without_leaking_path(tmp_path: Path) -> None:
    (tmp_path / "skill_script_test_sibling.py").write_text("VALUE = 42\n", encoding="utf-8")
    script = tmp_path / "entry.py"
    script.write_text("from skill_script_test_sibling import VALUE\n", encoding="utf-8")
    before = sys.path[:]

    try:
        module = load_skill_script(str(script))
        assert vars(module)["VALUE"] == 42
        assert sys.path == before
    finally:
        sys.modules.pop("entry_under_test", None)
        sys.modules.pop("skill_script_test_sibling", None)


def test_load_skill_script_restores_path_on_failure(tmp_path: Path) -> None:
    script = tmp_path / "broken.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "raise RuntimeError('import failed')\n",
        encoding="utf-8",
    )
    before = sys.path[:]

    try:
        with pytest.raises(RuntimeError, match="import failed"):
            load_skill_script(str(script))
        assert sys.path == before
    finally:
        sys.path[:] = before
        sys.modules.pop("broken_under_test", None)
