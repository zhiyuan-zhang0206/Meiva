from pathlib import Path

import base.host.system.probes as pp
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    fake_host,
)

_CHROME = "/usr/bin/google-chrome"
_NPX = "/usr/bin/npx"


def _linux(programs: dict[str, str] | None = None) -> Host:
    return fake_host(platform=LINUX_X86, programs=programs)


def _desktop_linux(programs: dict[str, str] | None = None) -> Host:
    return _linux(
        {"google-chrome": _CHROME, "npx": _NPX} if programs is None else programs,
    )


# ─── resolve_chrome_binary ───────────────────────────────────────────────


def test_resolve_uses_explicit_override(tmp_path: Path) -> None:
    """An explicit configured chrome binary is returned as-is, with NO exists()
    check (the browser daemon execs it and fails loudly if wrong)."""
    fake = tmp_path / "my-chrome"  # need not exist — returned verbatim
    assert pp.resolve_chrome_binary(str(fake), host=fake_host(platform=DARWIN_ARM64)) == str(fake)


def test_resolve_macos_default(tmp_path: Path) -> None:
    macos_chrome = tmp_path / "Google Chrome"
    macos_chrome.write_text("#!/bin/sh\n")
    host = fake_host(platform=DARWIN_ARM64)
    assert pp.resolve_chrome_binary(None, host=host, macos_chrome=macos_chrome) == str(macos_chrome)


def test_resolve_returns_none_when_missing(tmp_path: Path) -> None:
    macos_chrome = tmp_path / "absent-chrome"
    assert (
        pp.resolve_chrome_binary(
            None, host=fake_host(platform=DARWIN_ARM64), macos_chrome=macos_chrome
        )
        is None
    )


def test_resolve_linux_uses_which() -> None:
    host = _linux({"google-chrome": _CHROME})
    assert pp.resolve_chrome_binary(None, host=host) == _CHROME


def test_resolve_linux_falls_back_to_the_stable_channel_name() -> None:
    host = _linux({"google-chrome-stable": "/opt/chrome/stable"})
    assert pp.resolve_chrome_binary(None, host=host) == "/opt/chrome/stable"


def test_resolve_linux_none_when_not_on_path() -> None:
    assert pp.resolve_chrome_binary(None, host=_linux()) is None


# ─── display_available ───────────────────────────────────────────────────


def test_display_true_on_macos() -> None:
    assert pp.display_available(host=fake_host(platform=DARWIN_ARM64)) is True


def test_display_linux_requires_env() -> None:
    assert pp.has_display_vars(system="linux", display=None, wayland_display=None) is False
    assert pp.has_display_vars(system="linux", display="", wayland_display="") is False
    assert pp.has_display_vars(system="linux", display=":0", wayland_display=None) is True
    assert pp.has_display_vars(system="linux", display=None, wayland_display="wayland-0") is True
    assert pp.has_display_vars(system="darwin", display=None, wayland_display=None) is True


# ─── browser_incapability / browser_capable ──────────────────────────────


def test_incapability_none_when_all_present() -> None:
    host = _desktop_linux()
    assert pp.browser_incapability(None, host=host, has_display=True) is None
    assert pp.browser_capable(None, host=host, has_display=True) is True


def test_incapability_accepts_a_configured_chrome_without_one_on_the_path() -> None:
    host = _desktop_linux({"npx": _NPX})
    assert pp.browser_incapability("/opt/my-chrome", host=host, has_display=True) is None


def test_incapability_no_display() -> None:
    host = _desktop_linux()
    assert (
        pp.browser_incapability(None, host=host, has_display=False)
        == "no display (headless server)"
    )
    assert pp.browser_capable(None, host=host, has_display=False) is False


def test_incapability_no_chrome() -> None:
    host = _desktop_linux({"npx": _NPX})
    assert (
        pp.browser_incapability(None, host=host, has_display=True)
        == "no Chrome (install it or set MEIVA_CHROME_BINARY)"
    )
    assert pp.browser_capable(None, host=host, has_display=True) is False


def test_incapability_no_npx() -> None:
    host = _desktop_linux({"google-chrome": _CHROME})
    assert (
        pp.browser_incapability(None, host=host, has_display=True)
        == "no npx (install Node.js for chrome-devtools-mcp)"
    )
    assert pp.browser_capable(None, host=host, has_display=True) is False


def test_incapability_checks_display_first() -> None:
    """Order is display -> Chrome -> npx; the first missing prong wins, so a fully
    bare host reports the display reason (not a misleading Chrome/npx one)."""
    assert (
        pp.browser_incapability(None, host=_linux(), has_display=False)
        == "no display (headless server)"
    )


def test_npx_incapability_reason_is_exported_once() -> None:
    assert getattr(pp, "NPX_INCAPABILITY_REASON", None) == (
        "no npx (install Node.js for chrome-devtools-mcp)"
    )


# ─── permissions_helper_incapability ─────────────────────────────────────


def test_permissions_helper_is_macos_only() -> None:
    reason = pp.permissions_helper_incapability(host=_linux())
    assert reason == "macOS only (permissions helper drives the desktop)"


def test_permissions_helper_needs_the_swift_toolchain_and_signing() -> None:
    both = {"swiftc": "/usr/bin/swiftc", "codesign": "/usr/bin/codesign"}
    assert pp.permissions_helper_incapability(host=fake_host(platform=DARWIN_ARM64)) == (
        "no swiftc (install the Xcode command line tools)"
    )
    only_swift = fake_host(platform=DARWIN_ARM64, programs={"swiftc": both["swiftc"]})
    assert pp.permissions_helper_incapability(host=only_swift) == (
        "no codesign (install the Xcode command line tools)"
    )
    complete = fake_host(platform=DARWIN_ARM64, programs=both)
    assert pp.permissions_helper_incapability(host=complete) is None
