# meiva:note
# Read-only health probe for browser mcp; the root supervisor owns recovery.

import json
import socket
from pathlib import Path

from base.paths import chrome_mcp_socket
from services.desktop.browser.protocol import Request, Response

_TIMEOUT_S = 5.0


def browser_mcp_probe(home: Path, *, port: int) -> bool:
    """True when the daemon of `home` answers a lock-free ping over its socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        sock.connect(str(chrome_mcp_socket(home, port)))
        req: Request = {"id": 0, "method": "ping"}
        sock.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buf += chunk
        resp: Response = json.loads(buf.split(b"\n", 1)[0])
        return bool(resp.get("ok"))
    finally:
        sock.close()
