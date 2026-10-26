# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from base.deploy.release import collector_artifact as artifact


def _tarball(path: Path, *members: tuple[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return path


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def test_a_fresh_install_puts_an_executable_binary_and_leaves_no_staging(tmp_path: Path) -> None:
    dest_dir = tmp_path / "otel"
    dest_dir.mkdir()
    tarball = _tarball(
        tmp_path / "c.tar.gz", ("LICENSE", b"license"), ("otelcol-contrib", b"collector-v2")
    )

    artifact.install_binary(tarball, dest_dir)

    binary = dest_dir / artifact.BINARY_NAME
    assert binary.read_bytes() == b"collector-v2"
    assert binary.stat().st_mode & 0o777 == 0o755
    assert _names(dest_dir) == [artifact.BINARY_NAME]


def test_the_binary_is_found_below_a_directory_in_the_archive(tmp_path: Path) -> None:
    dest_dir = tmp_path / "otel"
    dest_dir.mkdir()
    tarball = _tarball(tmp_path / "c.tar.gz", ("dist/otelcol-contrib", b"nested"))

    artifact.install_binary(tarball, dest_dir)

    assert (dest_dir / artifact.BINARY_NAME).read_bytes() == b"nested"
    assert _names(dest_dir) == [artifact.BINARY_NAME]


def test_an_upgrade_replaces_the_entry_and_never_writes_into_the_running_binary(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "otel"
    dest_dir.mkdir()
    binary = dest_dir / artifact.BINARY_NAME
    binary.write_bytes(b"collector-v1")
    tarball = _tarball(tmp_path / "c.tar.gz", ("otelcol-contrib", b"collector-v2"))

    with binary.open("rb") as running:
        artifact.install_binary(tarball, dest_dir)
        assert running.read() == b"collector-v1"

    assert binary.read_bytes() == b"collector-v2"
    assert _names(dest_dir) == [artifact.BINARY_NAME]


def test_an_archive_without_the_binary_leaves_the_installed_one_alone(tmp_path: Path) -> None:
    dest_dir = tmp_path / "otel"
    dest_dir.mkdir()
    binary = dest_dir / artifact.BINARY_NAME
    binary.write_bytes(b"collector-v1")
    tarball = _tarball(tmp_path / "c.tar.gz", ("LICENSE", b"license"))

    with pytest.raises(RuntimeError, match="has no otelcol-contrib member"):
        artifact.install_binary(tarball, dest_dir)

    assert binary.read_bytes() == b"collector-v1"
    assert _names(dest_dir) == [artifact.BINARY_NAME]


def test_a_member_that_escapes_the_destination_fails_and_leaves_no_trace(tmp_path: Path) -> None:
    dest_dir = tmp_path / "otel"
    dest_dir.mkdir()
    binary = dest_dir / artifact.BINARY_NAME
    binary.write_bytes(b"collector-v1")
    tarball = _tarball(tmp_path / "c.tar.gz", ("../otelcol-contrib", b"escaped"))

    with pytest.raises(tarfile.FilterError):
        artifact.install_binary(tarball, dest_dir)

    assert binary.read_bytes() == b"collector-v1"
    assert _names(dest_dir) == [artifact.BINARY_NAME]
    assert not (tmp_path / "otelcol-contrib").exists()
