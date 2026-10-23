from __future__ import annotations

import os
import sys
from pathlib import Path

from agent.graph.exec import protocol


def die(*_args: object, **_kwargs: object) -> None:
    os._exit(9)


envelope = {
    "v": protocol.REQUEST_VERSION,
    "code": "x=1",
    "agent_id": 7,
    "context": {"identity": {"agent_id": 7, "owns_loop": True, "actor": None}},
    "timeout_s": 1.0,
}
if sys.argv[2] == "os.replace":
    protocol.write_json(Path(sys.argv[1]), envelope, replace=die)
elif sys.argv[2] == "os.fsync":
    protocol.write_json(Path(sys.argv[1]), envelope, fsync=die)
else:
    raise ValueError("Unknown envelope kill point")
