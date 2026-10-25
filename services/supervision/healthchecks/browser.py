"""Identity probe for the browser owned by the root supervisor."""

from collections.abc import Callable

from base.config import get_field
from base.daemon.health import DaemonProbe
from services.desktop.browser.probe import probe_browser


def _probe(*, read_port: Callable[[str], int] = get_field) -> DaemonProbe:
    """Report browser protocol health and ownership without changing processes."""
    return probe_browser(int(read_port("browser_cdp_port")))
