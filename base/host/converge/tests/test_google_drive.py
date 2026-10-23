import os
from pathlib import Path

import pytest

import base.host.converge.google_drive as gd

_skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses fs perms — readonly-dir test no-op as root"
)


def test_is_writable_true_and_cleans_up(tmp_path: Path) -> None:
    assert gd.is_writable(tmp_path) is True
    assert list(tmp_path.glob(".meiva_drive_check_*")) == []


def test_is_writable_false_for_missing_dir(tmp_path: Path) -> None:
    assert gd.is_writable(tmp_path / "nope") is False


def test_is_writable_false_for_file_target(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    assert gd.is_writable(f) is False


@_skip_if_root
def test_is_writable_false_for_readonly_dir(tmp_path: Path) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        assert gd.is_writable(d) is False
    finally:
        d.chmod(0o700)


def test_find_returns_first_writable_candidate(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    assert gd.find_writable_google_drive(candidates=[tmp_path / "missing", good]) == good


def test_find_none_when_no_candidate_writable(tmp_path: Path) -> None:
    assert gd.find_writable_google_drive(candidates=[tmp_path / "missing"]) is None


def test_find_none_when_no_candidates() -> None:
    assert gd.find_writable_google_drive(candidates=[]) is None


def test_macos_candidates_point_at_my_drive(tmp_path: Path) -> None:
    mount = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-someone@example.com" / "My Drive"
    mount.mkdir(parents=True)
    assert mount in gd.candidate_drive_dirs(home=tmp_path, system="darwin")


def test_macos_legacy_google_drive_folder(tmp_path: Path) -> None:
    (tmp_path / "Google Drive" / "My Drive").mkdir(parents=True)
    assert (tmp_path / "Google Drive" / "My Drive") in gd.candidate_drive_dirs(
        home=tmp_path, system="darwin"
    )


def test_linux_candidates_include_fixed_paths(tmp_path: Path) -> None:
    cands = gd.candidate_drive_dirs(home=tmp_path, system="linux")
    assert (tmp_path / "GoogleDrive") in cands


def test_unsupported_platform_returns_empty(tmp_path: Path) -> None:
    assert gd.candidate_drive_dirs(home=tmp_path, system="windows") == []
