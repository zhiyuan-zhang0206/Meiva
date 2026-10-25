"""Identity probe for the browser owned by the root supervisor."""

from base.config import get_field
from base.daemon.health import DaemonProbe
from services.desktop.browser.probe import probe_browser


def _probe(*, port: int | None = None) -> DaemonProbe:
    """Report browser protocol health and ownership without changing processes."""
    resolved_port = int(get_field("browser_cdp_port")) if port is None else port
    return probe_browser(resolved_port)
