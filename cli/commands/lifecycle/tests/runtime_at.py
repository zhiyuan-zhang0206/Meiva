# meiva:note
# A start runtime around a checkout a test built, holding the identity such a checkout reports.
#
# `StartRuntime.admit` verifies the checkout this process loaded, which a temporary repository
# is not; a lifecycle test that only needs a runtime value says which checkout it runs from
# and gets one whose loaded identity names it. A test that needs a specific identity (the one
# a serving-root double reports) replaces `loaded` on the result.

from __future__ import annotations

import sys
from pathlib import Path

from base.host.runtime_interpreter import LoadedRuntimeIdentity
from cli.start_runtime import StartRuntime

__all__ = ["runtime_at"]


def runtime_at(checkout: Path) -> StartRuntime:
    loaded = LoadedRuntimeIdentity(
        kind="source",
        code_root=str(checkout),
        interpreter=str(Path(sys.executable).resolve()),
        prefix=str(Path(sys.prefix).resolve()),
        cwd=str(checkout),
        source_digest="a" * 64,
    )
    return StartRuntime(checkout, checkout, Path(sys.executable).absolute(), loaded)
