from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from base.host.env.dotenv_boot import skip_config_fetch
from scripts.host_ops import check_worktree_remove

skip_config_fetch()

dialed: list[str] = []


def _audit(event: str, args: tuple[object, ...]) -> None:
    if event == "socket.connect" and len(args) >= 2 and not isinstance(args[1], str):
        dialed.append(f"{event}:{args[1]}")
        raise RuntimeError(f"{event} was called")
    if event == "urllib.Request":
        dialed.append(event)
        raise RuntimeError(f"{event} was called")


sys.addaudithook(_audit)
code = check_worktree_remove.main()
print("RESULT:" + json.dumps({"code": code, "dialed": dialed}))
