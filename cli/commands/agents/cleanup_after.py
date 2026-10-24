# meiva:note
# Run work, then its cleanup, with the work's failure outranking the cleanup's.
#
# A cleanup that fails after a failed body must not replace the failure the operator needs: it
# becomes a note on it and a warning in the log. A cleanup that fails after a body that
# succeeded is the failure, since nothing else went wrong.

from __future__ import annotations

import sys
from collections.abc import Awaitable

from base.log import logger

__all__ = ["run_then_clean_up"]


async def run_then_clean_up[T](body: Awaitable[T], clean_up: Awaitable[object], *, what: str) -> T:
    try:
        return await body
    finally:
        primary = sys.exception()
        try:
            await clean_up
        except Exception as cleanup_error:
            if primary is None:
                raise
            primary.add_note(f"{what} also failed: {cleanup_error!r}")
            logger.opt(exception=True).warning("{what} failed during failure", what=what)
