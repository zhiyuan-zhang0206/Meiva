# meiva:note
# Bounded read-only bytes with regular-file and read-stability checks.

import os
from pathlib import Path

from base.host.verified_file.plan import (
    read_limit,
    refusal_after_read,
    refusal_before_open,
    refusal_on_open,
    require_budget,
)


# meiva:note
# The file is not a stable regular file within its read budget.
class RegularFileReadError(ValueError):
    pass


# meiva:note
# Read one stable regular file within its caller's explicit format budget.
def regular_bytes(path: Path, *, max_bytes: int = 1024 * 1024) -> bytes:
    require_budget(max_bytes)
    info = path.lstat()
    refusal = refusal_before_open(path, info, max_bytes)
    if refusal is not None:
        raise RegularFileReadError(refusal)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        refusal = refusal_on_open(path, info, opened)
        if refusal is not None:
            raise RegularFileReadError(refusal)
        body = stream.read(read_limit(max_bytes))
        after = os.fstat(stream.fileno())
    refusal = refusal_after_read(
        path,
        body_length=len(body),
        max_bytes=max_bytes,
        opened=opened,
        after=after,
        current=path.lstat(),
    )
    if refusal is not None:
        raise RegularFileReadError(refusal)
    return body
