import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from base.host.proc import run_bounded
from scripts.audit import module_moves as gate
from scripts.audit.analysis import (
    is_text,
    module_pair,
    old_references,
    pair_result,
    referenced_names,
    verdict_line,
)


@pytest.fixture
def move_checkout(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    package = f"audit_move_{uuid4().hex}"
    source = tmp_path / package
    source.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "module.py").write_text("def present() -> None: ...\n", encoding="utf-8")
    run_bounded(
        ["git", "init", "--quiet"], timeout=10, capture_output=True, cwd=tmp_path
    ).check_returncode()
    owned_path = str(tmp_path)
    sys.path.insert(0, owned_path)
    importlib.invalidate_caches()
    try:
        yield tmp_path, package
    finally:
        sys.path.remove(owned_path)


def _audit(checkout: tuple[Path, str], files: dict[str, str], *, target: str | None = None) -> int:
    root, package = checkout
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run_bounded(["git", "add", "."], timeout=10, capture_output=True, cwd=root).check_returncode()
    return gate.main([f"pkg_old.mod_name={target or package + '.module'}"], repo_root=root)


def test_reports_dotted_parent_import_and_slash_lines(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {
                "references.txt": (
                    "import pkg_old.mod_name\n"
                    "from pkg_old import mod_name\n"
                    "See pkg_old/mod_name.py for details.\n"
                )
            },
        )
        == 1
    )
    output = capsys.readouterr().out
    assert [row for row in output.splitlines() if ": old reference" in row] == [
        f"references.txt:{line}: old reference to pkg_old.mod_name" for line in (1, 2, 3)
    ]
    assert "3 old references" in output
    assert output.endswith("AUDIT: FAIL\n")


def test_history_reads_ignore_quoted_and_bare_operands(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {
                "history.sh": (
                    'git show "$SHA:pkg_old/mod_name.py" > moved/module.py\n'
                    "git show bd6b15ed0:pkg_old/mod_name.py > moved/module.py\n"
                )
            },
        )
        == 0
    )
    assert "0 old references" in capsys.readouterr().out


def test_history_read_does_not_hide_an_old_destination(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {"history.sh": 'git show "abc:pkg_old/mod_name.py" > pkg_old/mod_name.py\n'},
        )
        == 1
    )
    assert "history.sh:1: old reference to pkg_old.mod_name" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text",
    [
        "pkg_old/sub/mod_name.py",
        "unrelated text",
        "mod_name.py",
        "pkg_old/mod_name.pyc",
        "pkg_old/mod_name.pyi",
        "other_pkg_old/mod_name.py",
        "other.pkg_old/mod_name.py",
    ],
)
def test_ignores_unrelated_or_inexact_paths(move_checkout: tuple[Path, str], text: str) -> None:
    assert _audit(move_checkout, {"references.txt": text}) == 0


def test_frozen_history_is_excluded_but_live_docs_fail(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {
                "docs/decisions/x.md": "pkg_old.mod_name\n",
                "docs/postmortems/y.md": "pkg_old.mod_name\n",
                "db/schema.sql": "-- pkg_old.mod_name\n",
                "base/packages/docs/notes.py": "# pkg_old.mod_name\n",
            },
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "docs/decisions/x.md:" not in output
    assert "docs/postmortems/y.md:" not in output
    assert "db/schema.sql:1: old reference" in output
    assert "base/packages/docs/notes.py:1: old reference" in output


def test_accepts_submodule_fallback_for_a_lean_package(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert (
        _audit(move_checkout, {"consumer.py": f"from {package} import module\n"}, target=package)
        == 0
    )
    assert "1 referenced names, missing=[]" in capsys.readouterr().out


def test_reports_unknown_names_at_the_new_module(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert (
        _audit(move_checkout, {"consumer.py": f"from {package}.module import no_such_name_xyz\n"})
        == 1
    )
    assert "missing=['no_such_name_xyz']" in capsys.readouterr().out


def test_existing_names_in_multiline_imports_and_quoted_targets_pass(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert (
        _audit(
            move_checkout,
            {
                "consumer.py": f"from {package}.module import (\n    present,\n)\n",
                "target.txt": f'"{package}.module.present"\n',
            },
        )
        == 0
    )
    assert "1 referenced names, missing=[]" in capsys.readouterr().out


def test_wildcard_import_fails_completeness(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert _audit(move_checkout, {"consumer.py": f"from {package}.module import *\n"}) == 1
    assert "missing=['*']" in capsys.readouterr().out


def test_import_failure_does_not_hide_remaining_pairs(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, package = move_checkout
    assert _audit(move_checkout, {}) == 0
    capsys.readouterr()
    assert (
        gate.main(
            [f"pkg_old.mod_name={package}.missing", f"pkg_old.other={package}.module"],
            repo_root=root,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "import failed: ModuleNotFoundError" in output
    assert f"pkg_old.other -> {package}.module: 0 old references" in output


@pytest.mark.parametrize("pair", ["missing_separator", "old.mod=new-mod", "old.mod="])
def test_invalid_move_pair_fails_at_the_cli_boundary(
    pair: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        gate.main([pair], repo_root=tmp_path)
    assert exit_info.value.code == 2
    assert "expected OLD=NEW with dotted Python module names" in capsys.readouterr().err


def test_old_references_counts_dotted_from_import_and_path_forms() -> None:
    text = "import pkg.old\nfrom pkg import old as later\npkg/old.py\n"
    assert old_references("pkg.old", text) == [1, 2, 3]


def test_old_references_ignore_a_pinned_history_object_read() -> None:
    text = "git show HEAD:pkg/old.py\nimport pkg.old\n"
    assert old_references("pkg.old", text) == [2]


def test_referenced_names_collects_attribute_and_from_import_names() -> None:
    text = "pkg.new.Alpha\nfrom pkg.new import Beta, Gamma\n"
    assert referenced_names("pkg.new", text) == {"Alpha", "Beta", "Gamma"}


def test_module_pair_accepts_only_two_dotted_module_names() -> None:
    assert module_pair("pkg.old=pkg.new") == ("pkg.old", "pkg.new")
    for invalid in ("missing_separator", "pkg.old=new-mod", "pkg.old=", "=pkg.new"):
        with pytest.raises(ValueError, match="expected OLD=NEW"):
            module_pair(invalid)


def test_is_text_rejects_a_binary_file_and_a_missing_read() -> None:
    assert is_text("import pkg.old\n")
    assert not is_text(None)
    assert not is_text("before\0after")


def test_pair_result_names_the_failure_of_one_pair() -> None:
    line, failed = pair_result(
        "pkg.old", "pkg.new", old_count=2, names=3, missing=["Gone"], import_error=""
    )
    assert line == "pkg.old -> pkg.new: 2 old references; 3 referenced names, missing=['Gone']"
    assert failed
    clean, ok = pair_result("pkg.old", "pkg.new", old_count=0, names=1, missing=[], import_error="")
    assert clean == "pkg.old -> pkg.new: 0 old references; 1 referenced names, missing=[]"
    assert not ok


def test_verdict_line_is_the_audit_result_the_exit_status_stands_for() -> None:
    assert verdict_line(failed=True) == "AUDIT: FAIL"
    assert verdict_line(failed=False) == "AUDIT: PASS"
