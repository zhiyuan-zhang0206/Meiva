# meiva:note
# What a bounded read of one stable regular file refuses, decided from the stats the reader
# observed: the budget, the entry before it is opened, the file once opened and the file after it
# was read. Each returns the refusal's text, or None when the read may go on. `verified_file.py`
# opens, stats, reads and raises.

from __future__ import annotations

import os
import stat
from pathlib import Path


# meiva:note
# Refuse a read budget that is not positive.
def require_budget(max_bytes: int) -> None:
    if max_bytes <= 0:
        raise ValueError("regular-file read budget must be positive")


# meiva:note
# How many bytes one read asks for: one past the budget, so a file that grew past it after it was
# checked comes back longer than the budget and `refusal_after_read` refuses it.
def read_limit(max_bytes: int) -> int:
    return max_bytes + 1


# meiva:note
# Why an entry is not read before it is opened: it is not a regular file, or it is over the budget.
def refusal_before_open(path: Path, info: os.stat_result, max_bytes: int) -> str | None:
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if info.st_size > max_bytes:
        return f"{path} is {info.st_size} bytes, over its {max_bytes}-byte read limit"
    return None


# meiva:note
# Why the opened file is not the entry that was checked: it is not a regular file, or it has another
# device or inode.
def refusal_on_open(path: Path, info: os.stat_result, opened: os.stat_result) -> str | None:
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        info.st_dev,
        info.st_ino,
    ):
        return f"{path} changed while opening"
    return None


# meiva:note
# Why what was read cannot be trusted: more than the budget came back, the file's size or mtime
# moved during the read, or the path now names another file than the one opened.
def refusal_after_read(
    path: Path,
    *,
    body_length: int,
    max_bytes: int,
    opened: os.stat_result,
    after: os.stat_result,
    current: os.stat_result,
) -> str | None:
    if (
        body_length > max_bytes
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        return f"{path} changed while reading"
    return None
