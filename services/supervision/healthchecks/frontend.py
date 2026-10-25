"""Frontend application readiness. Service recovery belongs to meiva-root."""

import subprocess
from collections.abc import Callable
from urllib.parse import urlsplit

from base.config import get_field
from base.daemon.health import DaemonProbe
from services.supervision.healthchecks.plans.frontend_plan import application_port


def _http_ok(port: int, *, curl_binary: str = "curl", timeout_s: float = 5.0) -> bool:
    """curl -fs probe; `-f` makes non-2xx exit non-zero, `-s` is silent."""
    try:
        result = subprocess.run(
            [curl_binary, "-fs", "-o", "/dev/null", f"http://localhost:{port}"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# meiva:note
# Probe the application on `port`, behind the entry gate.
def probe_frontend_at(
    port: int, *, curl_binary: str = "curl", timeout_s: float = 5.0
) -> DaemonProbe:
    if _http_ok(port, curl_binary=curl_binary, timeout_s=timeout_s):
        return DaemonProbe.up(f"frontend application on {port} answers HTTP")
    return DaemonProbe.down(f"frontend application on {port} is not ready")


# meiva:note
# Probe the application behind the entry gate, on the port the settings name.
def probe_frontend(*, read_field: Callable[[str], str | int | None] = get_field) -> DaemonProbe:
    raw_app_port = read_field("app_port")
    return probe_frontend_at(
        application_port(
            entry_port=urlsplit(str(read_field("frontend_healthcheck_url"))).port,
            app_port=int(raw_app_port) if raw_app_port is not None else None,
        )
    )
