# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

import os

from base.clock import SYSTEM_TIMERS
from base.config import ConfigBoot
from base.host.system.hostport import Host
from base.paths import meiva_home
from meiva.mcps.daemon_entry import BrowserRequest, serve
from services.desktop.browser.routing.boot import browser_routes_from_boot
from services.desktop.browser.routing.session import RoutedBrowserSession


def main() -> None:
    boot = ConfigBoot()
    boot.boot()
    routes = browser_routes_from_boot(
        boot, home=meiva_home(), timers=SYSTEM_TIMERS, host=Host.system(environ=os.environ)
    )

    def browser_factory(request: BrowserRequest) -> RoutedBrowserSession:
        return RoutedBrowserSession(routes, local_request=request)

    serve(browser_factory=browser_factory)


if __name__ == "__main__":
    main()
