import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from typing import Any, Literal, Self

import httpx
import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base import paths
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from gateway.app import create_app
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import GatewayConfig, gateway_config_from_boot

__all__ = [
    "SPAWN_PATH",
    "GatewayTestClient",
    "gateway_config_fields",
    "gateway_test_app",
    "gateway_test_client",
    "gateway_test_process",
    "post_spawn",
    "spawn_headers",
    "spawn_via_http",
]

SPAWN_PATH = "/api/keyed/v1/agents"
_CONFIG_FIELD_OVERRIDES: list[dict[str, Any]] = []


@contextmanager
def gateway_config_fields(**fields: Any) -> Generator[dict[str, Any]]:
    layer = dict(fields)
    _CONFIG_FIELD_OVERRIDES.append(layer)
    try:
        yield layer
    finally:
        _CONFIG_FIELD_OVERRIDES.pop()


def _resolve_boot(
    boot: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
) -> ConfigBoot:
    resolved = snapshot_process_config() if boot is None else boot
    for layer in _CONFIG_FIELD_OVERRIDES:
        for name, value in layer.items():
            resolved.set_field(name, value)
    if fields is not None:
        for name, value in fields.items():
            resolved.set_field(name, value)
    return resolved


@contextmanager
def gateway_test_process(
    *,
    machine: MachineIdentity | None = None,
    config: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
    process: GatewayProcess | None = None,
) -> Generator[GatewayProcess]:
    owned = process is None
    if owned:
        resolved_config = _resolve_boot(config, fields)
        resolved_machine = machine_from_boot(resolved_config) if machine is None else machine
        process = GatewayProcess(
            home=paths.meiva_home(),
            config=resolved_config,
            image=LOADED_IMAGE,
            machine=resolved_machine,
            host=fake_host(),
        )
    assert process is not None
    with process.lifetime() if owned else nullcontext(process):
        yield process


def gateway_test_app(
    *,
    process: GatewayProcess | None = None,
    config: GatewayConfig | None = None,
    boot: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
    machine: MachineIdentity | None = None,
    process_ownership: Literal["borrowed", "owned"] | None = None,
    cluster_secret: str | None = None,
    session_cookie_secure: bool | None = None,
    catalog: Any = None,
    login_limiter_clock: Callable[[], float] | None = None,
) -> FastAPI:
    if process is None:
        resolved_boot = _resolve_boot(boot, fields)
        resolved_machine = machine_from_boot(resolved_boot) if machine is None else machine
        process = GatewayProcess(
            home=paths.meiva_home(),
            config=resolved_boot,
            image=LOADED_IMAGE,
            machine=resolved_machine,
            host=fake_host(),
        )
        ownership: Literal["borrowed", "owned"] = (
            "owned" if process_ownership is None else process_ownership
        )
    else:
        ownership = "borrowed" if process_ownership is None else process_ownership
    resolved_config = (
        replace(gateway_config_from_boot(process.config), human_auth="required")
        if config is None
        else config
    )
    if cluster_secret is not None:
        # meiva:note
        # A test that pins the app's secret, e.g. to prove a client built from the
        # app carries it, instead of rewriting process-wide settings.
        resolved_config = replace(resolved_config, cluster_secret=cluster_secret)
    if session_cookie_secure is not None:
        resolved_config = replace(resolved_config, session_cookie_secure=session_cookie_secure)
    app = create_app(
        resolved_config,
        process=process,
        process_ownership=ownership,
    )
    if catalog is not None:
        app.state.catalog = catalog
    if login_limiter_clock is not None:
        app.state.login_limiter_clock = login_limiter_clock
    return app


class GatewayTestClient(TestClient):
    app: FastAPI

    def __enter__(self) -> Self:
        super().__enter__()
        return self


@contextmanager
def gateway_test_client(
    application: FastAPI | None = None,
    *,
    machine: MachineIdentity | None = None,
    config: GatewayConfig | None = None,
    boot: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
    process: GatewayProcess | None = None,
    cluster_secret: str | None = None,
    session_cookie_secure: bool | None = None,
    catalog: Any = None,
    login_limiter_clock: Callable[[], float] | None = None,
    **kwargs: Any,
) -> Generator[GatewayTestClient]:
    if application is not None:
        if any(
            arg is not None
            for arg in (
                machine,
                config,
                boot,
                fields,
                process,
                cluster_secret,
                session_cookie_secure,
                catalog,
                login_limiter_clock,
            )
        ):
            raise ValueError(
                "gateway_test_client(application, ...) borrows an already assembled FastAPI app; "
                "pass machine/config/boot/process to gateway_test_process or gateway_test_app instead."
            )
        with GatewayTestClient(application, **kwargs) as client:
            yield client
        return
    with gateway_test_process(
        machine=machine, config=boot, fields=fields, process=process
    ) as active_process:
        assembled = gateway_test_app(
            process=active_process,
            config=config,
            cluster_secret=cluster_secret,
            session_cookie_secure=session_cookie_secure,
            catalog=catalog,
            login_limiter_clock=login_limiter_clock,
        )
        with GatewayTestClient(assembled, **kwargs) as client:
            yield client


def spawn_headers(key: str | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return {
        **(extra or {}),
        "Idempotency-Key": key if key is not None else uuid.uuid4().hex,
        SCOPE_HEADER: PRINCIPAL_SCOPE,
    }


def spawn_via_http(
    client: TestClient,
    body: Mapping[str, Any] | None = None,
    *,
    key: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx2.Response:
    return client.post(SPAWN_PATH, json=dict(body or {}), headers=spawn_headers(key, headers))


def post_spawn(gateway_url: str, body: Mapping[str, Any], *, timeout: float) -> httpx.Response:
    return httpx.post(
        f"{gateway_url}{SPAWN_PATH}", json=dict(body), headers=spawn_headers(), timeout=timeout
    )
