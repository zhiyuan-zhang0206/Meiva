"""Read-only health probes for browser mcp; the root supervisor owns recovery."""

import json
import logging
import socket
from pathlib import Path

from base.config import get_field
from base.paths import chrome_mcp_socket
from services.desktop.browser.protocol import Request, Response

_log = logging.getLogger("services.supervision.healthchecks.browser_mcp")

_TIMEOUT_S = 5.0


def browser_mcp_probe(home: Path, *, port: int | None = None) -> bool:
    """True when the daemon of `home` answers a lock-free ping over its socket."""
    resolved_port = int(get_field("browser_cdp_port")) if port is None else port
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        sock.connect(str(chrome_mcp_socket(home, resolved_port)))
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


def is_alive(home: Path, *, port: int | None = None) -> bool:
    """True when the daemon of `home` answers a ping; ANY probe failure means "not alive".

    A healthcheck's contract with the watchdog is a bool, not an exception: an
    unforeseen probe failure degrades to "dead" — a decision the caller can act
    on — instead of to no answer at all. Unexpected types still log a traceback,
    so nothing is silently swallowed.
    """
    try:
        return browser_mcp_probe(home, port=port)
    except (OSError, json.JSONDecodeError):
        return False
    except Exception:
        _log.exception("[browser-mcp healthcheck] probe raised unexpectedly; treating as dead")
        return False
