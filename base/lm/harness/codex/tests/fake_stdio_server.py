# meiva:note
# A minimal JSON-RPC peer over stdio for the transport tests. The test drives it
# through the real StdioAppServer, so framing, id correlation and the approval
# round trip are exercised for real rather than through a fake object.

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast


def _bytes_param(message: dict[str, object]) -> int:
    params = message.get("params")
    if not isinstance(params, dict):
        return 0
    typed = cast("dict[str, object]", params)
    value = typed.get("bytes")
    return value if isinstance(value, int) else 0


def write_stderr(message: dict[str, object]) -> None:
    if message["method"] == "stderr_secret":
        value = os.environ["OPENAI_API_KEY"]
        _ = sys.stderr.write(f"OPENAI_API_KEY={value} diagnostic_value={value}\n")
    else:
        _ = sys.stderr.write("x" * _bytes_param(message) + "\nstartup diagnostic\n")
    sys.stderr.flush()


def hold_stderr(path: Path) -> None:
    for _ in range(500):
        if path.exists():
            return
        time.sleep(0.02)


def stderr_request(message: dict[str, object]) -> dict[str, object]:
    if message["method"] != "hold_stderr":
        write_stderr(message)
        return {}
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, __file__, "--hold-stderr", "release-stderr"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    return {"pid": child.pid}


def main() -> None:
    out = sys.stdout

    def send(message: dict[str, object]) -> None:
        out.write(json.dumps(message) + "\n")
        out.flush()

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake-stdio"}})
        elif method == "trace":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"trace": message.get("trace")}})
        elif method == "environment":
            send({"jsonrpc": "2.0", "id": request_id, "result": dict(os.environ)})
        elif method == "emit":
            send({"jsonrpc": "2.0", "method": "test/notification", "params": {"value": 1}})
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "ask":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "echo hi"},
                }
            )
            answer = json.loads(sys.stdin.readline())
            send({"jsonrpc": "2.0", "id": request_id, "result": {"answer": answer.get("result")}})
        elif method == "boom":
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "boom"}})
        elif method == "large_reply":
            size = _bytes_param(message)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"thread": {"id": "thread-big", "blob": "x" * size}},
                }
            )
        elif method == "eof_reply":
            size = _bytes_param(message)
            _ = out.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"eof": True, "blob": "y" * size},
                    }
                )
            )
            out.flush()
            return
        elif method == "unterminated_flood":
            size = _bytes_param(message)
            _ = out.write("x" * size)
            out.flush()
        elif method in ("stderr_secret", "stderr_flood", "hold_stderr"):
            send({"jsonrpc": "2.0", "id": request_id, "result": stderr_request(message)})
        elif method == "hang":
            continue
        elif method == "quit":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"echo": method}})


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--hold-stderr":
        hold_stderr(Path(sys.argv[2]))
    else:
        main()
