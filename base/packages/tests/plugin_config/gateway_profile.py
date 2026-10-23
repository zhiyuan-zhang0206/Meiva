# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
import os
import sys
from pathlib import Path
from typing import Literal

from base.host.proc import run_bounded


# meiva:note
# The fixed source module boots a real gateway profile in a private home.
# The only argument selects one of its two validation contracts.
def run_gateway_profile(*, home: Path, case: Literal["declaring-model", "validators"]) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            "-m",
            "base.packages.tests.plugin_config.gateway_profile_child",
            case,
        ],
        env={
            "PATH": os.defpath,
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_PROCESS_PROFILE": "gateway",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
