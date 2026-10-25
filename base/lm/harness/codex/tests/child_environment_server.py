from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# meiva:note
# A local app-server peer records its actual process environment and that of a
# short descendant. It never loads a provider SDK or contacts a model endpoint.
def main() -> None:
    if sys.argv[1:] == ["--environment"]:
        sys.stdout.write(json.dumps(dict(os.environ)) + "\n")
        return
    # meiva:note
    # Execute this known local script through the same interpreter with a literal
    # mode; neither the program nor its arguments come from a request or shell.
    descendant = subprocess.check_output(  # noqa: S603
        [sys.executable, __file__, "--environment"],
        text=True,
        timeout=5,
    )
    receipt = {
        "environment": dict(os.environ),
        "descendant": json.loads(descendant),
        "cwd": str(Path.cwd()),
    }
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message["method"]
        if method == "initialize":
            result = {"userAgent": "local-environment-peer"}
        elif method in {"thread/start", "thread/resume"}:
            receipt["method"] = method
            Path(os.environ["MEIVA_TEST_ENV_RECEIPT"]).write_text(json.dumps(receipt))
            thread_id = message["params"].get("threadId", "environment-thread")
            result = {"thread": {"id": thread_id, "cwd": str(Path.cwd())}}
        else:
            raise ValueError(f"unexpected local peer request: {method}")
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
