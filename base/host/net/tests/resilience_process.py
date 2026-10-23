from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Literal

Case = Literal["transports", "cli-help", "phase", "relative", "async-waits", "identity"]


# meiva:note
# The executable and module are fixed; the case selects one source-visible test operation.
def report(case: Case, *, agent_id: int = 0) -> dict[str, object]:
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "base.host.net.tests._resilience_child", case],
        env={
            "PATH": os.defpath,
            "MEIVA_HOME": os.environ["MEIVA_HOME"],
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_AGENT_ID": str(agent_id),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(process.stdout.strip().splitlines()[-1])
