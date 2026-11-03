"""Gate protocol readiness; recovery belongs to the lifecycle owner."""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import partial
from http.client import HTTPResponse
from pathlib import Path

from base.daemon.health import DaemonProbe
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from services.entrypoints.gate.daemon import entry_port


# meiva:note
# The first 4 KiB of the answer `opener(url)` gives. `urlopen` types its answer `Any`; a parameter
# typed with the real `HTTPResponse` takes it without naming the `Any`.
def _head(opener: Callable[[str], AbstractContextManager[HTTPResponse]], url: str) -> bytes:
    with opener(url) as response:
        return response.read(4096)


# meiva:note
# A Gate response proves its own readiness, independent of app availability. The response must
# name `home`: a Gate answering for another home holds the port.
def probe(*, home: Path) -> DaemonProbe:
    url = f"http://127.0.0.1:{entry_port()}/__meiva/healthz"
    try:
        payload = object_of(json.loads, _head(partial(urllib.request.urlopen, timeout=3), url))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return DaemonProbe.down(f"gate protocol unavailable: {exc}")
    if not is_object_dict(payload) or payload.get("name") != "gate":
        return DaemonProbe.down("gate health endpoint returned an invalid identity")
    if payload.get("home") != str(home):
        return DaemonProbe.port_taken("gate health endpoint belongs to another home")
    return DaemonProbe.up("gate health endpoint is ready")
