# meiva:note
# The CI samplers' captured, text-mode command contract without their writer imports.

import subprocess
from collections.abc import Sequence
from typing import Literal, Protocol

__all__ = ["GhProcess"]


class GhProcess(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: Literal[True],
        text: Literal[True],
    ) -> subprocess.CompletedProcess[str]: ...
