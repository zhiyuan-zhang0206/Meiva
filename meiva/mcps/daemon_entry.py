# meiva:link agent/docs/mcp-daemon.md

from ._browser_port import BrowserFactory as BrowserFactory
from ._browser_port import BrowserRequest as BrowserRequest


def serve(*, browser_factory: BrowserFactory) -> None:
    from ._daemon import main

    main(browser_factory=browser_factory)
