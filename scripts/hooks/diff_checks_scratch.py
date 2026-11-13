# meiva:note
# One pytest run's private scratch; removal never follows directory symlinks.

from __future__ import annotations

import os
import stat
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from scripts.hooks.diff_checks_pytest_child import PytestChildUnreapedError, PytestInterrupted


class ScratchCleanupError(RuntimeError):
    pass


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


def require_owned_directory(
    actual: os.stat_result, expected: os.stat_result, owner_uid: int
) -> None:
    if (
        actual.st_uid != owner_uid
        or expected.st_uid != owner_uid
        or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        or not stat.S_ISDIR(actual.st_mode)
    ):
        raise PermissionError("scratch directory owner or identity changed")


def _open_child(name: str, directory: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)


def _empty(
    directory: int, owner_uid: int, expected: os.stat_result, open_child: Callable[[str, int], int]
) -> None:
    require_owned_directory(os.fstat(directory), expected, owner_uid)
    os.fchmod(directory, os.fstat(directory).st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    for name in os.listdir(directory):
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child = open_child(name, directory)
            try:
                _empty(child, owner_uid, entry, open_child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)


def retain_for(error: BaseException) -> bool:
    return isinstance(error, PytestChildUnreapedError) or (
        isinstance(error, (PytestInterrupted, KeyboardInterrupt))
        and isinstance(error.__cause__, PytestChildUnreapedError)
    )


def _remove_owned(
    scratch: Path,
    parent: int,
    owned: int,
    identity: os.stat_result,
    open_child: Callable[[str, int], int],
) -> None:
    current = os.stat(scratch.name, dir_fd=parent, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
        raise ScratchCleanupError("owned scratch name was replaced")
    _empty(owned, identity.st_uid, identity, open_child)
    os.rmdir(scratch.name, dir_fd=parent)


def _cleanup_failure(scratch: Path, error: BaseException, primary: BaseException | None) -> None:
    diagnostic = f"pytest scratch cleanup incomplete: {scratch}: {type(error).__name__}"
    print(diagnostic, file=sys.stderr)
    if primary is None:
        raise ScratchCleanupError(diagnostic) from error
    primary.add_note(diagnostic)


@contextmanager
def pytest_scratch(
    parent: Path, *, open_child: Callable[[str, int], int] | None = None
) -> Generator[Path]:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError(f"pytest scratch parent must be private and owned: {parent}")
        scratch = Path(tempfile.mkdtemp(prefix="diff-checks-", dir=parent))
        owned = os.open(
            scratch.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        identity = os.fstat(owned)
        retained = False
        primary: BaseException | None = None
        try:
            yield scratch
        except BaseException as error:
            primary = error
            retained = retain_for(error)
            if retained:
                print(
                    f"pytest child exit unconfirmed; retained scratch: {scratch}", file=sys.stderr
                )
            raise
        finally:
            try:
                if not retained:
                    _remove_owned(scratch, directory, owned, identity, open_child or _open_child)
            except (OSError, RuntimeError) as error:
                _cleanup_failure(scratch, error, primary)
            finally:
                os.close(owned)
    finally:
        os.close(directory)
