# meiva:note
# Locks `scripts/lint/host_platform_marks.py`: a test names the host it needs, or injects one.
# meiva:link scripts/lint/docs/host-platform-marks.meiva.okf.md

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from scripts.lint import host_platform_marks as _lint

_PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'


def _findings(src: str) -> list[str]:
    return [message for _, _, message in _lint.findings_in_source(src)]


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


@pytest.mark.parametrize("read", ["is_macos()", "plat.is_linux()"])
def test_the_tree_own_host_predicates_are_findings(read: str) -> None:
    # meiva:note
    # The predicates reach `sys.platform` underneath; exempting them by name would leave
    # the same read one call deeper.
    found = _findings(f"def test_x():\n    return {read}\n")
    assert len(found) == 1
    assert read.rsplit(".", maxsplit=1)[-1] in found[0]


@pytest.mark.parametrize(
    "read", ["system()", "platform()", "uname()", "release()", "mac_ver()", "win32_ver()"]
)
def test_the_bounded_platform_module_calls_are_findings(read: str) -> None:
    # meiva:note
    # `platform` is a closed set: these six answer "what host is this", so the vocabulary
    # is finite and the rule has no ceiling here.
    found = _findings(f"import platform\n\n\ndef test_x():\n    assert platform.{read}\n")
    assert len(found) == 1 and f"`platform.{read}`" in found[0]


@pytest.mark.parametrize("read", ["python_version()", "python_implementation()", "machine()"])
def test_the_interpreter_and_architecture_calls_are_not_findings(read: str) -> None:
    # meiva:note
    # The interpreter version is not a host fact, and `machine()` is the architecture —
    # no marker carries that dimension, so counting it would only produce noise.
    assert _findings(f"import platform\n\n\ndef test_x():\n    return platform.{read}\n") == []


def test_a_dynamic_attribute_read_is_outside_the_rule() -> None:
    # meiva:note
    # The documented boundary, kept in step with the document: `getattr` hides the name
    # from any static rule, so a passing test here is the honest state, not a gap.
    assert (
        _findings("import platform\n\n\ndef test_x():\n    return getattr(platform, 'system')()\n")
        == []
    )


def test_os_name_is_a_finding() -> None:
    found = _findings("import os\n\n\ndef test_x():\n    assert os.name == 'posix'\n")
    assert len(found) == 1 and "`os.name`" in found[0]


def test_an_import_skip_is_a_finding() -> None:
    found = _findings("import pytest\n\n\ndef test_x():\n    pytest.importorskip('zstd')\n")
    assert len(found) == 1 and "`pytest.importorskip(...)`" in found[0]


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
    assert [line for line, _, _ in _lint.findings_in_source(src)] == [4]


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
    baseline.write_text(json.dumps({"sys.platform": 1}), encoding="utf-8")
    assert _lint.main([], repo_root=root) == 0

    test.write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n"
        "def test_y():\n    assert sys.platform\n",
        encoding="utf-8",
    )
    assert _lint.main([], repo_root=root) == 1


def test_a_recorded_count_that_shrank_fails_until_it_is_lowered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # The record and the tree are one fact: the change that removes a read lowers the number in
    # the same diff. Left as a note, the reduction outlives its reads and the next branch
    # carrying this record inherits the work (2026-10-24: two merged PRs left their reductions
    # behind, and a third PR's merge-tree run failed instead).
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n", encoding="utf-8"
    )
    baseline = root / _lint.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"sys.platform": 2}), encoding="utf-8")
    assert _lint.main([], repo_root=root) == 1
    assert "below the recorded 2" in capsys.readouterr().err


def test_renaming_a_file_that_carries_a_recorded_read_is_not_a_finding(
    tmp_path: Path,
) -> None:
    # meiva:note
    # A per-path baseline reddened on a rename, which is how a gate gets switched off. The
    # count follows the read, not the file, so moving the same code changes nothing.
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n", encoding="utf-8"
    )
    baseline = root / _lint.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"sys.platform": 1}), encoding="utf-8")
    assert _lint.main([], repo_root=root) == 0

    (root / "tests" / "test_x.py").rename(root / "tests" / "test_renamed.py")
    assert _lint.main([], repo_root=root) == 0


def test_a_site_outside_the_change_is_marked_as_nobodys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # The count is the whole tree's, so a `--only` run can name a file the author never
    # opened. Saying which sites are theirs is the difference between a gate and a mystery.
    root = _repo(tmp_path)
    (root / "tests" / "test_mine.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_theirs.py").write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n", encoding="utf-8"
    )
    baseline = root / _lint.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"sys.platform": 0}), encoding="utf-8")
    assert _lint.main(["--only", "tests/test_mine.py"], repo_root=root) == 1
    err = capsys.readouterr().err
    assert "tests/test_theirs.py:5" in err
    assert "[outside your change]" in err


def test_a_scoped_run_judges_the_whole_tree_but_prints_its_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # `--only` is the pre-commit path: it must still catch a read added to the file under
    # review, so the comparison runs over the whole tree.
    root = _repo(tmp_path)
    (root / "tests" / "test_a.py").write_text(
        "import sys\n\n\ndef test_a():\n    assert sys.platform\n", encoding="utf-8"
    )
    baseline = root / _lint.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"sys.platform": 1}), encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "import sys\n\n\ndef test_a():\n    assert sys.platform\n"
        "def test_b():\n    assert sys.platform\n",
        encoding="utf-8",
    )
    assert _lint.main(["--only", "tests/test_a.py"], repo_root=root) == 1
    assert "tests/test_a.py:7" in capsys.readouterr().err


def test_write_baseline_records_the_current_findings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "import sys\n\n\ndef test_x():\n    assert sys.platform\n", encoding="utf-8"
    )
    assert _lint.main(["--write-baseline"], repo_root=root) == 0
    written = json.loads((root / _lint.BASELINE).read_text(encoding="utf-8"))
    assert written == {"sys.platform": 1}
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


# meiva:note
# ── the exemption record: a reviewed exception, not a comment ──────────────

_HOST_READ = (
    "import sys\n\n\n"
    "def test_x():\n"
    "    # host-ok: the subject is the launchd lane (`tests/fixtures/host_platform.py`)\n"
    "    assert sys.platform\n"
)


def _exemptions(root: Path, entries: dict[str, str], allowed: int | None = None) -> None:
    path = root / _lint.EXEMPTIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"allowed": len(entries) if allowed is None else allowed, "exemptions": entries}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _exempt_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(_HOST_READ, encoding="utf-8")
    return root


def test_a_recorded_grounded_exemption_is_honoured(tmp_path: Path) -> None:
    root = _exempt_repo(tmp_path)
    _exemptions(
        root,
        {
            "tests/test_x.py::test_x": "the subject is the launchd lane (`tests/fixtures/host_platform.py`)"
        },
    )
    assert _lint.main([], repo_root=root) == 0


def test_an_unrecorded_exemption_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # The comment alone was a free pass: nobody reviews a line they do not expect.
    root = _exempt_repo(tmp_path)
    assert _lint.main([], repo_root=root) == 1
    assert "unrecorded exemption" in capsys.readouterr().err


def test_a_reason_that_names_nothing_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "import sys\n\n\ndef test_x():\n    # host-ok: it is hard to port\n    assert sys.platform\n",
        encoding="utf-8",
    )
    _exemptions(root, {"tests/test_x.py::test_x": "it is hard to port"})
    assert _lint.main([], repo_root=root) == 1
    assert "names no path or job" in capsys.readouterr().err


def test_a_reason_may_cite_the_ci_job_that_runs_it(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "import sys\n\n\ndef test_x():\n    # host-ok: helper-signing-smoke runs this\n"
        "    assert sys.platform\n",
        encoding="utf-8",
    )
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  helper-signing-smoke:\n    name: Helper signing\n", encoding="utf-8"
    )
    _exemptions(root, {"tests/test_x.py::test_x": "helper-signing-smoke runs this"})
    assert _lint.main([], repo_root=root) == 0


def test_a_record_entry_without_its_site_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    _exemptions(root, {"tests/test_x.py::test_x": "gone (`tests/test_x.py`)"})
    assert _lint.main([], repo_root=root) == 1
    assert "no longer in the tree" in capsys.readouterr().err


def test_the_record_count_is_frozen(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _exempt_repo(tmp_path)
    _exemptions(
        root,
        {
            "tests/test_x.py::test_x": "the subject is the launchd lane (`tests/fixtures/host_platform.py`)"
        },
        allowed=3,
    )
    assert _lint.main([], repo_root=root) == 1
    assert "allowed says 3" in capsys.readouterr().err


def test_write_baseline_records_the_exemption(tmp_path: Path) -> None:
    root = _exempt_repo(tmp_path)
    assert _lint.main(["--write-baseline"], repo_root=root) == 0
    written = json.loads((root / _lint.EXEMPTIONS).read_text(encoding="utf-8"))
    assert written == {
        "allowed": 1,
        "exemptions": {
            "tests/test_x.py::test_x": "the subject is the launchd lane (`tests/fixtures/host_platform.py`)"
        },
    }
    assert _lint.main([], repo_root=root) == 0


def test_the_marker_inside_a_string_is_not_an_exemption() -> None:
    # meiva:note
    # A fixture that writes the marker into a string is data; only a comment is a claim.
    assert _lint.exemptions_in_source('X = "# host-ok: not mine"\n') == []


# meiva:note
# One parse answers both questions. A whole-tree run parses every test file it walks
# (2026-10-29: 3192 files, 21.4 MB), so the findings and the exemptions must not each pay
# for a tree of their own. The counting parser is the assertion: a second `ast.parse`
# inside `judge_source` fails here, and no production symbol is patched to see it.
def test_one_parse_yields_both_verdicts() -> None:
    src = (
        "import sys\n\n\n"
        "def test_flagged():\n"
        "    assert sys.platform == 'darwin'\n\n\n"
        "def test_exempt():\n"
        "    assert sys.platform == 'darwin'  "
        "# host-ok: the subject is the launchd lane (`tests/fixtures/host_platform.py`)\n"
    )
    parsed: list[str] = []

    def counting_parse(text: str) -> ast.Module:
        parsed.append(text)
        return ast.parse(text)

    findings, exemptions = _lint.judge_source(src, parse=counting_parse)
    assert parsed == [src]
    assert [what for _, what, _ in findings] == ["sys.platform"]
    assert exemptions == [
        ("test_exempt", "the subject is the launchd lane (`tests/fixtures/host_platform.py`)")
    ]


def test_a_file_that_does_not_parse_contributes_nothing() -> None:
    assert _lint.judge_source("def broken(\n") == ([], [])


def test_a_marker_without_a_reason_is_still_read() -> None:
    # meiva:note
    # The gate is the marker's own text, so its shortest spelling has to reach the reader
    # too: the marker with nothing after its colon exempts with an empty reason, and the
    # record layer is what rejects a reason that names nothing. Narrowing the gate to one
    # that also demands a space after the colon would silently stop reading this line.
    src = "def test_x():\n    assert sys.platform == 'darwin'  # host-ok:\n"
    assert _lint.exemptions_in_source(src) == [("test_x", "")]
    assert _lint.judge_source(src)[0] == []


def test_the_whole_tree_path_parses_a_file_once() -> None:
    # meiva:note
    # `judge_source` takes its parser as a parameter, so a second parse written next to the
    # first would not show in a count of the calls it makes. The reading below catches both
    # spellings: a direct `ast.parse` and a second use of the parameter.
    body = inspect.getsource(_lint.judge_source)
    assert "ast.parse(" not in body
    assert body.count("parse(text)") == 1
