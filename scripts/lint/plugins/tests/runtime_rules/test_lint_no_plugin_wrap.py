from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint.plugins import no_plugin_wrap as gate


def test_directory_with_unreadable_member_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = tmp_path / gate.SCAN_DIR
    plugins.mkdir(parents=True)
    (plugins / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (plugins / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    (plugins / "dangling.py").symlink_to(plugins / "missing.py")
    assert gate.main([str(plugins / "bad_utf8.py")], repo_root=tmp_path) == 0
    assert gate.main([], repo_root=tmp_path) == 0
    (plugins / "viol.py").write_text("meiva.files.read = my_read\n", encoding="utf-8")
    assert gate.main([], repo_root=tmp_path) == 1
    assert "meiva.files.read" in capsys.readouterr().out


def test_explicit_plugins_directory_argument_enumerates_members(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = tmp_path / gate.SCAN_DIR
    plugins.mkdir(parents=True)
    (plugins / "ok.py").write_text("value = 1\n", encoding="utf-8")
    assert gate.main([str(plugins)], repo_root=tmp_path) == 0
    (plugins / "viol.py").write_text("meiva.files.read = my_read\n", encoding="utf-8")
    assert gate.main([str(plugins)], repo_root=tmp_path) == 1
    assert "meiva.files.read" in capsys.readouterr().out


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)], repo_root=tmp_path) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)], repo_root=tmp_path) == 1


def test_default_scope_is_the_builtin_plugins_tree() -> None:
    plugins = gate.REPO_ROOT / gate.SCAN_DIR
    assert plugins.is_dir()
    assert any(plugins.rglob("plugin.py"))


def test_missing_default_scan_dir_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="meiva_builtins/plugins"):
        gate.main([], repo_root=tmp_path)
