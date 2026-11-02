# meiva:note
# A real tree under `tmp_path`, sealed with the platform's own `chmod` (and `chflags` on macOS):
# the manifest describes it exactly, every later change shows up as a difference, and a symlink
# out of the tree is never followed.

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from base.deploy.sealed_release import sealed_plan, sealed_tree
from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86, fake_host

_POSIX = fake_host(commands=run_bounded, platform=LINUX_X86)


def _tree(root: Path, outside: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    outside.write_text("interpreter\n")
    (root / "python").symlink_to(outside)
    return root


def _sealed(tmp_path: Path, sealed_paths: list[tuple[Path, Host]], host: Host = _POSIX) -> Path:
    release = _tree(tmp_path / "releases" / "1-abc", tmp_path / "outside")
    sealed_paths.append((release, host))
    sealed_paths.append((sealed_plan.manifest_path(release), host))
    sealed_tree.seal(release, host=host)
    sealed_tree.write_manifest(release, sealed_tree.snapshot(release), host=host)
    return release


def test_a_sealed_tree_verifies_clean(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    assert sealed_tree.verify(release, content=False) == ()
    assert sealed_tree.verify(release, content=True) == ()


def test_sealing_removes_every_write_bit_inside_the_tree(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    manifest = sealed_tree.read_manifest(release)
    assert {
        key
        for key, entry in manifest.entries.items()
        if entry.kind is not sealed_plan.EntryKind.LINK and entry.mode & 0o222
    } == set()
    assert stat.S_IMODE(sealed_plan.manifest_path(release).stat().st_mode) == 0o444


def test_a_symlink_out_of_the_tree_is_recorded_not_followed(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    assert stat.S_IMODE((tmp_path / "outside").stat().st_mode) & 0o200
    entry = sealed_tree.read_manifest(release).entries["python"]
    assert (entry.kind, entry.target, entry.sha256) == (
        sealed_plan.EntryKind.LINK,
        str(tmp_path / "outside"),
        None,
    )


# meiva:note
# Unsealing a file to write it moves its ctime and the write moves its size and mtime; a new file
# is an added path and a deleted one a missing path. `lstat` alone finds all three.
def test_changes_after_sealing_are_found_without_reading_content(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    sealed_tree.unseal(release, host=_POSIX)
    with (release / "pkg" / "mod.py").open("a") as handle:
        handle.write("VALUE = 2\n")
    (release / "pkg" / "extra.py").write_text("")
    (release / "run.sh").unlink()

    findings = sealed_tree.verify(release, content=False)

    assert "added: pkg/extra.py" in findings
    assert "missing: run.sh" in findings
    changed = next(line for line in findings if line.startswith("changed: pkg/mod.py "))
    assert "size" in changed and "ctime_ns" in changed


def test_only_the_content_check_compares_bytes(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    manifest_file = sealed_plan.manifest_path(release)
    sealed_tree.unseal(manifest_file, host=_POSIX)
    body = json.loads(manifest_file.read_text())
    body["entries"]["pkg/mod.py"][7] = "0" * 64
    manifest_file.write_text(json.dumps(body))

    assert sealed_tree.verify(release, content=False) == ()
    assert sealed_tree.verify(release, content=True) == ("changed: pkg/mod.py (sha256)",)


def test_a_tree_found_at_another_path_is_a_finding(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    alias = tmp_path / "alias"
    alias.symlink_to(release)
    assert sealed_tree.verify(alias, content=False) == ()
    manifest_file = sealed_plan.manifest_path(release)
    sealed_tree.unseal(manifest_file, host=_POSIX)
    body = json.loads(manifest_file.read_text())
    body["root"] = "/somewhere/else"
    manifest_file.write_text(json.dumps(body))
    assert sealed_tree.verify(release, content=False) == (
        f"moved: sealed at /somewhere/else, found at {release}",
    )


def test_remove_deletes_a_sealed_release_and_its_manifest(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    release = _sealed(tmp_path, sealed_paths)
    sealed_tree.remove(release, host=_POSIX)
    assert not release.exists()
    assert not sealed_plan.manifest_path(release).exists()


def test_verifying_without_a_manifest_raises(tmp_path: Path) -> None:
    release = _tree(tmp_path / "releases" / "1-abc", tmp_path / "outside")
    with pytest.raises(FileNotFoundError):
        sealed_tree.verify(release, content=False)


# meiva:note
# On macOS the immutable flag refuses `chmod` back to writable, writes, new entries in a
# directory and unlinking; `chflags -R nouchg` is the only way back, and it moves ctime too.
@pytest.mark.requires_host("darwin")
def test_on_macos_a_sealed_tree_refuses_writes_and_new_entries(
    tmp_path: Path, sealed_paths: list[tuple[Path, Host]]
) -> None:
    host = fake_host(commands=run_bounded, platform=DARWIN_ARM64)
    release = _sealed(tmp_path, sealed_paths, host)
    target = release / "pkg" / "mod.py"
    with pytest.raises(PermissionError):
        target.chmod(0o644)
    with pytest.raises(PermissionError):
        target.write_text("tampered\n")
    with pytest.raises(PermissionError):
        (release / "pkg" / "new.py").write_text("")
    with pytest.raises(PermissionError):
        target.unlink()
    sealed_tree.unseal(release, host=host)
    assert any(
        line.startswith("changed: pkg/mod.py ")
        for line in sealed_tree.verify(release, content=False)
    )
