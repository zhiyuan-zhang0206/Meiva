import random
import shlex
from pathlib import Path

import pytest
from pydantic import ValidationError

from base.cluster.derive import fe_build_env, frontend_service_cmd
from base.config import ConfigBoot
from base.config.domains.gateway import GatewaySettings
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from gateway.http.middleware.cors import (
    cors_allowed_origins,
    session_cookie_secure,
    trusted_request_hosts,
)


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


def _config(owner: ConfigBoot) -> GatewayConfig:
    return gateway_config_from_boot(
        owner,
        user_home=Path("/nonexistent/browser-entries-home"),
        home=Path("/nonexistent/browser-entries-meiva-home"),
        memory_root=Path("/nonexistent/browser-entries-memory"),
        rng=_rng(),
    )


_FIRST = "https://public.example"
_SECOND = "https://private.example:8443"


@pytest.mark.parametrize(
    "value",
    [
        "http://console.example",
        "https://user:pass@console.example",
        "https://console.example/api",
        "https://console.example?x=1",
        "https://console.example#x",
        "https://console.example:99999",
        "https://console.example:",
        "https://console .example",
        "https://console.example,",
        "https://one.example,,https://two.example",
        "https://one.example,http://two.example",
        "https://one.example,https://ONE.example:443",
        "console.example",
    ],
)
def test_browser_entries_refuse_anything_but_plain_https_origins(value: str) -> None:
    with pytest.raises(ValidationError, match="browser origin"):
        GatewaySettings.model_validate({"browser_origins": value})


def test_browser_entries_normalize_each_origin_in_the_order_written() -> None:
    parsed = GatewaySettings.model_validate(
        {"browser_origins": "https://Console.Example:443/, https://Other.Example:8443"}
    )

    assert parsed.browser_origins == ["https://console.example", "https://other.example:8443"]


def test_browser_entries_default_to_none() -> None:
    assert GatewaySettings.model_validate({"browser_origins": ""}).browser_origins == []
    assert GatewaySettings.model_validate({}).browser_origins == []


def test_build_env_and_frontend_command_carry_every_entry() -> None:
    env = dict(
        item.split("=", 1)
        for item in shlex.split(fe_build_env(gateway_port=8800, browser_origins=(_FIRST, _SECOND)))
    )
    assert env == {
        "NEXT_PUBLIC_GATEWAY_PORT": "8800",
        "NEXT_PUBLIC_BROWSER_ORIGINS": f"{_FIRST},{_SECOND}",
    }
    command = shlex.split(
        frontend_service_cmd(3001, gateway_port=8800, browser_origins=(_FIRST, _SECOND))
    )
    assert f"NEXT_PUBLIC_BROWSER_ORIGINS={_FIRST},{_SECOND}" in command
    assert command[command.index("--browser-origins") + 1] == f"{_FIRST},{_SECOND}"


def test_build_env_without_an_entry_names_no_browser_origin() -> None:
    assert fe_build_env(gateway_port=8800, browser_origins=()) == "NEXT_PUBLIC_GATEWAY_PORT=8800"
    assert "--browser-origins" not in frontend_service_cmd(
        3001, gateway_port=8800, browser_origins=()
    )


def test_cors_and_trusted_hosts_use_every_entry_without_changing_gateway() -> None:
    owner = ConfigBoot()
    owner.set_field("browser_origins", [_FIRST, _SECOND])
    owner.set_field("gateway_url", "http://192.0.2.2:8800")
    owner.set_field("cors_allowed_origins", [])
    cfg = _config(owner)
    assert _FIRST in cfg.allowed_origins
    assert _SECOND in cfg.allowed_origins
    assert "http://192.0.2.2:8800" in cfg.allowed_origins
    assert owner.view.gateway.gateway_url == "http://192.0.2.2:8800"
    assert {"public.example", "private.example"} <= cfg.trusted_hosts


def test_explicit_cors_remains_authoritative() -> None:
    owner = ConfigBoot()
    owner.set_field("browser_origins", [_FIRST, _SECOND])
    owner.set_field("app_port", 18069)
    owner.set_field("cors_allowed_origins", ["https://other.example"])
    assert _config(owner).allowed_origins == ("https://other.example",)
    assert cors_allowed_origins(
        explicit=["https://other.example"],
        frontend_healthcheck_url="http://localhost:18055",
        app_port=18069,
        browser_origins=(_FIRST, _SECOND),
        gateway_url="",
    ) == ["https://other.example"]


def test_trusted_hosts_follow_the_derived_origins() -> None:
    hosts = trusted_request_hosts(
        allowed_origins=cors_allowed_origins(
            explicit=(),
            frontend_healthcheck_url="http://localhost:18055",
            app_port=None,
            browser_origins=(_FIRST, _SECOND),
            gateway_url="",
        ),
        machine_host="192.0.2.2",
        gateway_url="",
    )

    assert {"public.example", "private.example"} <= hosts
    assert "other.example" not in hosts


@pytest.mark.parametrize(
    ("request_url", "secure"),
    [
        ("https://public.example/api/auth/login", True),
        ("https://public.example:443/api/auth/login", True),
        ("https://private.example:8443/api/auth/login", True),
        ("https://private.example/api/auth/login", False),
        ("https://other.example/api/auth/login", False),
        ("http://public.example/api/auth/login", False),
        ("http://192.0.2.2:8800/api/auth/login", False),
        ("", False),
    ],
    ids=[
        "first-entry",
        "first-entry-explicit-default-port",
        "second-entry",
        "second-entry-wrong-port",
        "not-an-entry",
        "entry-host-over-http",
        "direct-http-listener",
        "no-request-url",
    ],
)
def test_https_cookie_policy_is_secure_on_any_entry_and_preserves_direct_http(
    request_url: str, secure: bool
) -> None:
    owner = ConfigBoot()
    owner.set_field("browser_origins", [_FIRST, _SECOND])
    owner.set_field("gateway_url", "http://192.0.2.2:8800")
    owner.set_field("session_cookie_secure", None)
    cfg = _config(owner)
    assert not cfg.session_cookie_secure

    assert (
        session_cookie_secure(
            request_url,
            browser_origins=cfg.browser_origins,
            default_secure=cfg.session_cookie_secure,
        )
        is secure
    )
