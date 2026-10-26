"""Identity probe for the browser owned by the root supervisor."""

from pathlib import Path

from base.daemon.health import DaemonProbe
from services.desktop.browser.probe import probe_browser


# meiva:note
# Report browser protocol health and ownership on CDP `port` for the Chrome profile `profile`
# without changing processes.
def browser_probe(port: int, profile: Path) -> DaemonProbe:
    return probe_browser(port, profile)
