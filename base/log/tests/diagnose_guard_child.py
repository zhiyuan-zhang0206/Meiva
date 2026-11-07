# meiva:note
# Child process for the diagnose guard: one interpreter per way a process ends up with a loguru
# handler, each holding the same sentinel in a failing frame's local variable.
#
#   own-sink   -- a sink opened through `add_sink`, which is what an exec host, the MCP daemon, the
#                 pty session server and the exec child do; loguru's own default stderr handler is
#                 still registered when it opens, so this is the shape that used to render values.
#   raw-add    -- a sink added by calling `logger.add` directly. The lint refuses that call outside
#                 `base/log/`; this file keeps it so the test can show the guard sees such a handler
#                 -- without it, a guard that removed every handler would pass the other mode.

from __future__ import annotations

import sys

from base.log import logger
from base.log.diagnose import diagnosing_handler_ids

SENTINEL = "SENTINEL-PASSWORD-123"


def _fail(value: str) -> None:
    raise RuntimeError("boom")


# meiva:note
# `diagnose` renders the value of a name the failing line uses, so the sentinel has to sit on that
# line rather than merely exist nearby -- a local the traceback never mentions is not rendered even
# with diagnose on, and the test would pass for the wrong reason.
def _raise_with_the_sentinel_on_the_frame() -> None:
    db_password = SENTINEL
    _fail(db_password)


def main(mode: str) -> None:
    if mode == "own-sink":
        from base.log.sinks import add_sink

        _ = add_sink(sys.stderr, format="{message}", level="INFO")
    elif mode == "raw-add":
        _ = logger.add(sys.stderr, format="{message}", level="INFO")
    else:
        raise SystemExit(f"unknown mode: {mode}")

    _ = sys.stdout.write(f"diagnosing handlers: {diagnosing_handler_ids()}\n")
    try:
        _raise_with_the_sentinel_on_the_frame()
    except RuntimeError:
        logger.opt(exception=True).error("caught")


if __name__ == "__main__":
    main(sys.argv[1])
