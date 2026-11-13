# meiva:note
# One pytest run's private scratch; removal never follows directory symlinks.

from __future__ import annotations

import os
import stat
import sys
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from scripts.hooks.diff_checks_pytest_child import PytestChildUnreapedError


def pytest_scratch_parent(environ: Mapping[str, str]) -> Path:
    # meiva:note
    # Use the existing reaper's explicit temp root and USER path convention.
    # meiva:note
    # existing reaper root
    temp_root = Path(environ.get("TMPDIR") or "/tmp")  # noqa: S108
    user = environ.get("USER", "unknown")
    if not temp_root.is_absolute() or not user or user in (".", "..") or Path(user).name != user:
        raise ValueError("pytest scratch needs an absolute temp root and one user path component")
    return temp_root / f"pytest-of-{user}"


def _empty(directory: int) -> None:
    os.fchmod(directory, os.fstat(directory).st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    for name in os.listdir(directory):
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _empty(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)


@contextmanager
def pytest_scratch(parent: Path) -> Generator[Path]:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError(f"pytest scratch parent must be private and owned: {parent}")
        scratch = Path(tempfile.mkdtemp(prefix="pytest-diff-checks-", dir=parent))
        owned = os.open(
            scratch.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        identity = os.fstat(owned)
        retained = False
        try:
            yield scratch
        except PytestChildUnreapedError:
            retained = True
            print(f"pytest child exit unconfirmed; retained scratch: {scratch}", file=sys.stderr)
            raise
        finally:
            try:
                current = os.stat(scratch.name, dir_fd=directory, follow_symlinks=False)
                if retained:
                    pass
                elif (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    print(
                        f"pytest scratch cleanup incomplete: replaced name: {scratch}",
                        file=sys.stderr,
                    )
                else:
                    _empty(owned)
                    os.rmdir(scratch.name, dir_fd=directory)
            except (OSError, RuntimeError) as error:
                print(
                    f"pytest scratch cleanup incomplete: {scratch}: {type(error).__name__}",
                    file=sys.stderr,
                )
            finally:
                os.close(owned)
    finally:
        os.close(directory)
