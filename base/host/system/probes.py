from __future__ import annotations

import os
from pathlib import Path

from base.host.system.hostport import Host

MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_MACOS_CHROME_PATH = Path(MACOS_CHROME)
_LINUX_CHROME_NAMES = ("google-chrome", "google-chrome-stable")
NPX_INCAPABILITY_REASON = "no npx (install Node.js for chrome-devtools-mcp)"


def platform_chrome_binary(*, host: Host, macos_chrome: Path = _MACOS_CHROME_PATH) -> str | None:
    """Resolve a platform-default Chrome binary without reading Settings."""
    if host.platform.system == "darwin":
        return str(macos_chrome) if macos_chrome.exists() else None
    for name in _LINUX_CHROME_NAMES:
        found = host.which(name)
        if found:
            return found
    return None


def resolve_chrome_binary(
    configured_chrome: str | None,
    *,
    host: Host,
    macos_chrome: Path = _MACOS_CHROME_PATH,
) -> str | None:
    """Resolve a Chrome binary path for this host, or None if none is found.

    Resolution order:
    - explicit ``configured_chrome`` override (``settings.services.chrome_binary``) -> returned **as-is** (no
      existence check; a caller that execs it fails loudly at exec time if the
      path is wrong, which is the intended fail-fast for the browser daemon).
    - platform-default Chrome binary -> resolved by ``platform_chrome_binary()``.
    """
    if configured_chrome:
        return configured_chrome
    return platform_chrome_binary(host=host, macos_chrome=macos_chrome)


def has_display_vars(
    *,
    system: str,
    display: str | None,
    wayland_display: str | None,
) -> bool:
    if system == "darwin":
        return True
    return bool(display or wayland_display)


def display_available(*, host: Host) -> bool:
    """Return True if a window server a headed browser can use is reachable.

    macOS: always True. Known limitation: a headless macOS server (SSH-only, no
    logged-in desktop) cannot be cheaply distinguished from a headed one via
    env-vars alone, so macOS is treated as always having a display.

    Linux: ``$DISPLAY`` (X11) or ``$WAYLAND_DISPLAY`` must be set and non-empty
    (a headless server has neither).

    ``Platform`` admits only macOS and Linux, so there is no other case.
    """
    return has_display_vars(
        system=host.platform.system,
        display=os.environ.get("DISPLAY"),
        wayland_display=os.environ.get("WAYLAND_DISPLAY"),
    )


def decide_browser_incapability(
    *,
    has_display: bool,
    chrome_binary: str | None,
    npx_binary: str | None,
) -> str | None:
    if not has_display:
        return "no display (headless server)"
    if chrome_binary is None:
        return "no Chrome (install it or set MEIVA_CHROME_BINARY)"
    if npx_binary is None:
        return NPX_INCAPABILITY_REASON
    return None


def browser_incapability(
    configured_chrome: str | None,
    *,
    host: Host,
    has_display: bool | None = None,
    macos_chrome: Path = _MACOS_CHROME_PATH,
) -> str | None:
    """Return why this host cannot run a headed Chrome, or None if it can.

    The single source of truth for the three-prong capability check (display +
    Chrome binary + npx), evaluated in that order. The returned string is
    operator-facing -- it surfaces in ``meiva status``, the ``meiva start`` /
    watchdog / warmup skip logs, and the RuntimeError the browser daemon raises
    at launch -- so each reason names the fix, not just the missing prong.

    ``browser_capable()`` (the bool gate) and
    ``services.desktop.browser.daemon.assert_browser_capable()`` (the raising preflight)
    both derive from this, so the capability logic lives in exactly one place.
    """
    effective_display = display_available(host=host) if has_display is None else has_display
    return decide_browser_incapability(
        has_display=effective_display,
        chrome_binary=resolve_chrome_binary(
            configured_chrome, host=host, macos_chrome=macos_chrome
        ),
        npx_binary=host.which("npx"),
    )


def browser_capable(
    configured_chrome: str | None,
    *,
    host: Host,
    has_display: bool | None = None,
    macos_chrome: Path = _MACOS_CHROME_PATH,
) -> bool:
    """Return True if this host can run a headed Chrome (display + binary + npx).

    Thin bool wrapper over ``browser_incapability()`` for the gates that only
    need yes/no (the service roster's start decision, the host-config validator).
    Callers that want to show or log *why* call ``browser_incapability()``.
    """
    return (
        browser_incapability(
            configured_chrome,
            host=host,
            has_display=has_display,
            macos_chrome=macos_chrome,
        )
        is None
    )


def permissions_helper_incapability(*, host: Host, has_display: bool = True) -> str | None:
    if host.platform.system == "darwin":
        if host.which("swiftc") is None:
            return "no swiftc (install the Xcode command line tools)"
        if host.which("codesign") is None:
            return "no codesign (install the Xcode command line tools)"
        if not (has_display and display_available(host=host)):
            return "no display (the permissions helper drives an on-screen desktop)"
        return None
    return "macOS only (permissions helper drives the desktop)"
