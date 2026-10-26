from __future__ import annotations

from pathlib import Path

import pytest

from services.entrypoints.gate.launch_plan import GateFacts, app_port_of, gateway_base, plan_launch

_FACTS = GateFacts(
    entry_port=3000,
    app_port=3001,
    gateway_port=8000,
    gateway_host="gateway-box",
    browser_origin="",
    default_static_dir=Path("/checkout/static"),
)


def test_the_settings_decide_when_the_command_line_names_nothing() -> None:
    launch = plan_launch(port=None, static_dir=None, facts=_FACTS)
    assert launch.port == 3000
    assert launch.static_dir == Path("/checkout/static")
    assert launch.app_base == "http://127.0.0.1:3001"
    assert launch.gateway_base == "http://gateway-box:8000"
    assert launch.browser_origin == ""


def test_the_command_line_wins_over_the_settings() -> None:
    launch = plan_launch(port=4100, static_dir=Path("/pages"), facts=_FACTS)
    assert (launch.port, launch.static_dir) == (4100, Path("/pages"))
    assert launch.app_base == "http://127.0.0.1:3001"


def test_the_browser_origin_is_carried_through() -> None:
    facts = GateFacts(
        entry_port=3000,
        app_port=3001,
        gateway_port=8000,
        gateway_host="gateway-box",
        browser_origin="https://console.example",
        default_static_dir=Path("static"),
    )
    assert plan_launch(port=None, static_dir=None, facts=facts).browser_origin == (
        "https://console.example"
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", "http://localhost:8000"),
        ("10.64.0.7", "http://10.64.0.7:8000"),
        ("fd00::7", "http://[fd00::7]:8000"),
        ("[fd00::7]", "http://[fd00::7]:8000"),
    ],
)
def test_an_ipv6_host_is_bracketed_once(host: str, expected: str) -> None:
    assert gateway_base(host=host, port=8000) == expected


def test_the_app_port_is_the_configured_one_or_the_slot_after_the_entry() -> None:
    assert app_port_of(None, entry_port=3000) == 3001
    assert app_port_of(4800, entry_port=3000) == 4800
    assert app_port_of(0, entry_port=4100) == 4101
