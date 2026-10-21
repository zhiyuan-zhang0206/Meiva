# meiva:note
# Locks `scripts/lint/host_platform_marks.py`: a test names the host it needs, or injects one.
# meiva:link scripts/lint/docs/host-platform-marks.meiva.okf.md

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.lint import host_platform_marks as _lint

_PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'


def _findings(src: str) -> list[str]:
    return [message for _, message in _lint.findings_in_source(src)]


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    return tmp_path


def test_reading_the_process_platform_is_a_finding() -> None:
    src = "import sys\n\n\ndef test_x():\n    assert sys.platform == 'darwin'\n"
    found = _findings(src)
    assert len(found) == 1
    assert "`sys.platform`" in found[0]
    assert "requires_host" in found[0]


def test_building_the_real_host_is_a_finding() -> None:
    found = _findings("def test_x():\n    host = Host.system()\n    assert host\n")
    assert "`Host.system()`" in found[0]


@pytest.mark.parametrize("read", ["Host.system()", "Platform.current()"])
def test_every_constructor_of_the_real_host_is_covered(read: str) -> None:
    assert _findings(f"def test_x():\n    return {read}\n")


def test_resolving_through_the_ambient_path_is_a_finding() -> None:
    found = _findings("import shutil\n\n\ndef test_x():\n    assert shutil.which('uv')\n")
    assert "`shutil.which(...)`" in found[0]


def test_an_explicit_search_path_is_not_a_finding() -> None:
    src = "import shutil\n\n\ndef test_x():\n    assert shutil.which('uv', path=manager['PATH'])\n"
    assert _findings(src) == []


def test_the_marker_on_the_test_covers_it() -> None:
    src = (
        "import sys\nimport pytest\n\n\n"
        '@pytest.mark.requires_host("darwin")\n'
        "def test_x():\n    assert sys.platform == 'darwin'\n"
    )
    assert _findings(src) == []


def test_a_module_level_pytestmark_covers_the_module() -> None:
    src = (
        "import sys\nimport pytest\n\n"
        'pytestmark = pytest.mark.requires_host("darwin")\n\n\n'
        "def test_x():\n    assert sys.platform == 'darwin'\n"
    )
    assert _findings(src) == []


def test_the_exemption_covers_the_function_it_sits_in() -> None:
    src = (
        "import sys\n\n\n"
        '@pytest.mark.skipif(False, reason="x")\n'
        "def test_x():\n"
        "    # host-ok: this test asserts that PATH was emptied\n"
        "    assert sys.platform != 'nope'\n"
    )
    assert _findings(src) == []


def test_the_exemption_says_where_it_stops_at_module_level() -> None:
    src = "import sys\n\nFLAG = sys.platform == 'darwin'\nOTHER = sys.platform == 'linux'\n"
    src = src.replace("FLAG", "FLAG  # host-ok: asserted by the launchd lane")
    assert [line for line, _ in _lint.findings_in_source(src)] == [4]


def test_a_marker_inside_a_class_covers_its_methods() -> None:
    src = (
        "import sys\nimport pytest\n\n\n"
        '@pytest.mark.requires_host("linux")\n'
        "class TestGroup:\n"
        "    def test_x(self):\n        assert sys.platform == 'linux'\n"
    )
    assert _findings(src) == []


def test_a_baseline_entry_allows_exactly_its_count(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    test = root / "tests" / "test_x.py"
    test.write_text("import sys\n\n\ndef test_x():\n    assert sys.platform\n", encoding="utf-8")
    baseline = root / _lint.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"tests/test_x.py": 1}), encoding="utf-8")
    assert _lint.main([], repo_root=root) == 0

    test.write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n"
        "def test_y():\n    assert sys.platform\n",
        encoding="utf-8",
    )
    assert _lint.main([], repo_root=root) == 1


def test_a_baseline_entry_for_a_deleted_file_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    baseline = root / _lint.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"tests/gone.py": 2}), encoding="utf-8")
    assert _lint.main([], repo_root=root) == 1
    assert "no longer exists" in capsys.readouterr().err


def test_write_baseline_records_the_current_findings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n", encoding="utf-8"
    )
    assert _lint.main(["--write-baseline"], repo_root=root) == 0
    written = json.loads((root / _lint.BASELINE).read_text(encoding="utf-8"))
    assert written == {"tests/test_x.py": 1}
    assert _lint.main([], repo_root=root) == 0


def test_write_baseline_refuses_a_partial_scan(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert _lint.main(["--write-baseline", "--only", "tests/test_x.py"], repo_root=root) == 1


def test_an_explicit_path_outside_a_tests_directory_is_not_judged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "base").mkdir()
    offender = root / "base" / "x.py"
    offender.write_text("import sys\nx = sys.platform\n", encoding="utf-8")
    assert _lint.main([str(offender)], repo_root=root) == 0
