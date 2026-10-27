"""Gate protocol readiness; recovery belongs to the lifecycle owner."""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from base.daemon.health import DaemonProbe
from services.entrypoints.gate.daemon import entry_port


# meiva:note
# A Gate response proves its own readiness, independent of app availability. The response must
# name `home`: a Gate answering for another home holds the port.
def probe(*, home: Path) -> DaemonProbe:
    url = f"http://127.0.0.1:{entry_port()}/__meiva/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read(4096))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return DaemonProbe.down(f"gate protocol unavailable: {exc}")
    payload_any: Any = payload
    if not isinstance(payload, dict):
        return DaemonProbe.down("gate health endpoint returned an invalid identity")
    typed_payload: dict[str, Any] = payload_any
    if typed_payload.get("name") != "gate":
        return DaemonProbe.down("gate health endpoint returned an invalid identity")
    if typed_payload.get("home") != str(home):
        return DaemonProbe.port_taken("gate health endpoint belongs to another home")
    return DaemonProbe.up("gate health endpoint is ready")
