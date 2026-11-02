# meiva:note
# Staging a commit of a real throwaway clone into a home, switching the home's `source` to it,
# and reading the switched tree's identity without Git. The Python build is replaced by a stand-in
# that writes one file, so the test needs `git` and the platform's `chmod`, not `uv` or a network.

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from base.deploy.sealed_release import sealed_plan, sealed_tree, stage
from base.deploy.sealed_release.stage_plan import StagePlanError
from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import LINUX_X86, fake_host
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import RELEASE_RECORD, LoadedCommit

_HOST = fake_host(commands=run_bounded, platform=LINUX_X86)


# meiva:note
# S603: git with fixed arguments in a throwaway clone.
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _clone(path: Path) -> Path:
    (path / "pkg").mkdir(parents=True)
    _git(path, "init", "--initial-branch=main")
    (path / "pkg" / "mod.py").write_text("VALUE = 1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "first")
    (path / "pkg" / "mod.py").write_text("VALUE = 2\n")
    _git(path, "commit", "-am", "second")
    return path


def _build(release: Path) -> None:
    (release / ".venv").mkdir()
    (release / ".venv" / "built").write_text("ok\n")


def _stage(tmp_path: Path, sealed_paths: list[tuple[Path, Host]], commit: str = "HEAD") -> Path:
    repo = tmp_path / "clone" if (tmp_path / "clone").exists() else _clone(tmp_path / "clone")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    release = stage.stage(repo, commit, home=home, host=_HOST, build=_build)
    sealed_paths.append((release, _HOST))
    sealed_paths.append((sealed_plan.manifest_path(release), _HOST))
    return release


def test_a_staged_release_is_the_commit_without_git_plus_its_build(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _stage(tmp_path, sealed_paths)
    head = _git(tmp_path / "clone", "rev-parse", "HEAD")

    assert release == (tmp_path / "home" / "source-releases" / f"2-{head[:12]}").resolve()
    assert not (release / ".git").exists()
    assert (release / "pkg" / "mod.py").read_text() == "VALUE = 2\n"
    assert (release / ".venv" / "built").exists()
    assert sealed_tree.verify(release, content=True) == ()


def test_the_switched_source_names_its_commit_and_version_without_git(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _stage(tmp_path, sealed_paths)
    home = tmp_path / "home"
    assert stage.switch(home, release) == release

    link = home / "source"
    assert link.is_symlink()
    assert str(link.readlink()) == f"source-releases/{release.name}"
    image = LoadedCommit.capture(link.resolve())
    assert image.sha == _git(tmp_path / "clone", "rev-parse", "HEAD")
    assert CodeVersion(image).get() == 2


def test_restaging_returns_the_sealed_release_untouched(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    first = _stage(tmp_path, sealed_paths)
    inode = first.stat().st_ino
    assert _stage(tmp_path, sealed_paths) == first
    assert first.stat().st_ino == inode


def test_an_unfinished_stage_is_rebuilt(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _stage(tmp_path, sealed_paths)
    sealed_tree.unseal(sealed_plan.manifest_path(release), host=_HOST)
    sealed_plan.manifest_path(release).unlink()
    sealed_tree.unseal(release, host=_HOST)
    (release / "stray.txt").write_text("left by a stopped stage\n")

    assert _stage(tmp_path, sealed_paths) == release
    assert not (release / "stray.txt").exists()
    assert sealed_tree.verify(release, content=False) == ()


def test_a_manifest_left_without_its_tree_is_rebuilt(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _stage(tmp_path, sealed_paths)
    sealed_tree.unseal(release, host=_HOST)
    shutil.rmtree(release)

    assert _stage(tmp_path, sealed_paths) == release
    assert sealed_tree.verify(release, content=False) == ()


def test_a_shallow_clone_is_refused(tmp_path: Path) -> None:
    full = _clone(tmp_path / "full")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth", "1", f"file://{full}", str(shallow))
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(stage.StageError, match="shallow clone"):
        stage.stage(shallow, "HEAD", home=home, host=_HOST, build=_build)


def test_a_release_changed_since_sealing_is_refused(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _stage(tmp_path, sealed_paths)
    sealed_tree.unseal(release / "pkg", host=_HOST)
    (release / "pkg" / "mod.py").write_text("VALUE = 3\n")

    with pytest.raises(stage.StageError, match="changed since it was sealed"):
        _stage(tmp_path, sealed_paths)
    with pytest.raises(stage.StageError, match="differs from its manifest"):
        stage.switch(tmp_path / "home", release)


def test_switching_replaces_the_pointer_and_never_a_checkout(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    older = _stage(tmp_path, sealed_paths, "HEAD~1")
    newer = _stage(tmp_path, sealed_paths)
    home = tmp_path / "home"
    stage.switch(home, older)
    stage.switch(home, newer)
    assert (home / "source").resolve() == newer
    assert not (home / ".source.next").exists()
    (home / ".source.next").symlink_to("left-by-a-stopped-switch")
    stage.switch(home, older)
    assert (home / "source").resolve() == older

    (home / "source").unlink()
    (home / "source").mkdir()
    with pytest.raises(stage.StageError, match="one-time cutover"):
        stage.switch(home, newer)


def test_a_directory_outside_the_homes_releases_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "source-releases").mkdir(parents=True)
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    with pytest.raises(stage.StageError, match="not a directory of this home"):
        stage.switch(home, stranger)


def test_the_release_source_runs_from_is_never_rebuilt(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _stage(tmp_path, sealed_paths)
    home = tmp_path / "home"
    stage.switch(home, release)
    sealed_tree.unseal(sealed_plan.manifest_path(release), host=_HOST)
    sealed_plan.manifest_path(release).unlink()

    with pytest.raises(StagePlanError, match="unfinished release"):
        _stage(tmp_path, sealed_paths)
    assert (release / RELEASE_RECORD).exists()


def test_the_verify_entry_exits_nonzero_on_a_difference(
    tmp_path: Path,
    sealed_paths: list[tuple[Path, Host]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = _stage(tmp_path, sealed_paths)
    assert stage.main(["verify", "--release", str(release)], host=_HOST) == 0
    sealed_tree.unseal(release, host=_HOST)
    (release / "added.txt").write_text("")
    assert stage.main(["verify", "--release", str(release)], host=_HOST) == 1
    assert "added: added.txt" in capsys.readouterr().out
