"""Frontend application readiness. Service recovery belongs to meiva-root."""

import subprocess

from base.daemon.health import DaemonProbe


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
