# meiva:link services/desktop/browser/docs/operations.md

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW


def workspace_path_for(workspace: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Chrome file path must name an entry inside the agent workspace")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Chrome file path escapes the agent workspace")
    path = workspace / path
    if not path.is_relative_to(workspace) or path == workspace:
        raise ValueError("Chrome file path escapes the agent workspace")
    return path


@contextmanager
def directory_fd(path: Path) -> Generator[int, None, None]:
    fd = os.open(path.anchor, _DIRECTORY)
    try:
        for segment in path.parts[1:]:
            child = os.open(segment, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_parent(root: int, parts: tuple[str, ...], *, create: bool) -> Generator[int, None, None]:
    current = os.dup(root)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=current)
            child = os.open(part, _DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def read_workspace_file(workspace: Path, path: Path, *, limit: int) -> bytes:
    parts = path.relative_to(workspace).parts
    try:
        with (
            directory_fd(workspace) as root,
            open_parent(root, parts[:-1], create=False) as parent,
            os.fdopen(os.open(parts[-1], _READ_FILE, dir_fd=parent), "rb") as stream,
        ):
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Chrome transfer requires a regular workspace file")
            data = stream.read(limit + 1)
    except OSError as exc:
        raise ValueError(
            "Chrome workspace file could not be opened without following symlinks"
        ) from exc
    if len(data) > limit:
        raise ValueError(f"Chrome file transfer exceeds the {limit} byte limit")
    return data


def write_workspace_file(workspace: Path, path: Path, data: bytes) -> None:
    parts = path.relative_to(workspace).parts
    try:
        with directory_fd(workspace) as root, open_parent(root, parts[:-1], create=True) as parent:
            publish_at(parent, parts[-1], data)
    except OSError as exc:
        raise ValueError("Chrome output could not be published inside the workspace") from exc


def publish_at(parent: int, dest: str, data: bytes) -> None:
    name = ".chrome-output-" + os.urandom(16).hex()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            _ = stream.write(data)
        os.replace(name, dest, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)
