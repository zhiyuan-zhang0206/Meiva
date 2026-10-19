# meiva:note
# A minimal JSON-RPC peer over stdio for the transport tests. The test drives it
# through the real StdioAppServer, so framing, id correlation and the approval
# round trip are exercised for real rather than through a fake object.

from __future__ import annotations

import json
import sys


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
        elif method == "hang":
            continue
        elif method == "quit":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"echo": method}})


if __name__ == "__main__":
    main()
