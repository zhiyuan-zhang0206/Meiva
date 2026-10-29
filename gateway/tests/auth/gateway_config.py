import random
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Literal, Self, cast

import httpx
import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base import paths
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.deploy.lifecycle import start_serving
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugin_providers import build_model_catalog
from gateway.app import create_app
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from gateway.http.model_catalog import load_model_catalog
from gateway.tests.auth.gateway_auth import gateway_credentials
from ops.cluster.rpc import RpcClient

__all__ = [
    "SPAWN_PATH",
    "GatewayTestClient",
    "anonymous_gateway_client",
    "gateway_config_fields",
    "gateway_config_of",
    "gateway_test_app",
    "gateway_test_client",
    "gateway_test_config",
    "gateway_test_process",
    "gateway_test_rpc",
    "post_spawn",
    "reconfigure",
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


# meiva:note
# The cluster RPC client a gateway root builds, over the test process's configuration snapshot and
# the system timers: its credential and dispatch budget are the ones the stub runners expect.
def gateway_test_rpc() -> RpcClient:
    return RpcClient.from_boot(snapshot_process_config(), timers=SYSTEM_TIMERS, rng=seeded_rng())


# meiva:note
# The gateway config a test derives from `boot`: the process's own account home and a seeded
# generator, which makes every id and session key the app draws repeat from run to run.
def gateway_config_of(boot: ConfigBoot) -> GatewayConfig:
    return gateway_config_from_boot(
        boot,
        user_home=Path.home(),
        memory_root=paths.gateway_memory_dir(
            roles=ConfiguredMachineIdentity(boot).role(), home=paths.resolve_meiva_home()
        ),
        rng=random.Random(0),  # noqa: S311
    )


# meiva:note
# The app config a test client gets by default (human auth required), with `changes`
# applied: a tuning value a test shrinks or a limit it lowers, passed as a `GatewayConfig`
# field to `gateway_test_client(config=...)`. The positional `fields` are configuration fields applied to the
# owner the config is derived from, for a setting that is a value of the config
# (`gateway_test_config({"memory_rerank_candidates": 40})`); pass the same `fields` to the client so the
# process the app runs on holds them too.
def gateway_test_config(
    fields: Mapping[str, Any] | None = None, /, **changes: Any
) -> GatewayConfig:
    return replace(gateway_config_of(_resolve_boot(None, fields)), human_auth="required", **changes)


# meiva:note
# Replace the frozen config of a running test app with a copy that has `changes` applied. A
# handler reads `app.state.gateway_config` at each request, so the next request sees the new
# value; use it where one test needs two values of one tunable, and build a second app
# (`gateway_test_client(config=...)`) where the tunable must hold from the start. Only the config
# is replaced: `app.state.clock` was built from the original `config.clock` and is not rebuilt, so
# a test that varies the clock configuration builds a second app too.
def reconfigure(client: TestClient, **changes: Any) -> GatewayConfig:
    app = cast(FastAPI, client.app)
    app.state.gateway_config = replace(app.state.gateway_config, **changes)
    return app.state.gateway_config


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
    home: Path | None = None,
    machine: MachineIdentityReader | None = None,
    config: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
    process: GatewayProcess | None = None,
    observe: Callable[[Path], start_serving.RootBirth] = start_serving.observe_root,
) -> Generator[GatewayProcess]:
    owned = process is None
    if owned:
        resolved_config = _resolve_boot(config, fields)
        resolved_machine = (
            ConfiguredMachineIdentity(resolved_config) if machine is None else machine
        )
        process = GatewayProcess(
            home=paths.meiva_home() if home is None else home,
            config=resolved_config,
            image=LOADED_IMAGE,
            machine=resolved_machine,
            host=fake_host(),
            observe=observe,
        )
    assert process is not None
    with process.lifetime() if owned else nullcontext(process):
        yield process


def gateway_test_app(
    *,
    home: Path | None = None,
    process: GatewayProcess | None = None,
    config: GatewayConfig | None = None,
    boot: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
    machine: MachineIdentityReader | None = None,
    process_ownership: Literal["borrowed", "owned"] | None = None,
    cluster_secret: str | None = None,
    session_cookie_secure: bool | None = None,
    catalog: Any = None,
) -> FastAPI:
    if process is None:
        resolved_boot = _resolve_boot(boot, fields)
        resolved_machine = ConfiguredMachineIdentity(resolved_boot) if machine is None else machine
        process = GatewayProcess(
            home=paths.meiva_home() if home is None else home,
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
        replace(gateway_config_of(process.config), human_auth="required")
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
    return create_app(
        resolved_config,
        process=process,
        process_ownership=ownership,
        catalog=load_model_catalog(partial(build_model_catalog, home=process.home))
        if catalog is None
        else catalog,
    )


# meiva:note
# A test client over a gateway app. It carries the app's cluster secret as its bearer unless
# the test gives its own `Authorization` header or asks for an anonymous client
# (`credentials=False`); the default is read from the app's own config at build time, so a
# config carrying a rotated secret yields a client carrying it.
class GatewayTestClient(TestClient):
    app: FastAPI

    def __init__(self, app: FastAPI, *args: Any, credentials: bool = True, **kwargs: Any) -> None:
        config = getattr(app.state, "gateway_config", None)
        if credentials and isinstance(config, GatewayConfig):
            headers = dict(kwargs.get("headers") or {})
            if not any(name.lower() == "authorization" for name in headers):
                headers.update(gateway_credentials(config))
            kwargs["headers"] = headers
        super().__init__(app, *args, **kwargs)

    def __enter__(self) -> Self:
        super().__enter__()
        return self


@contextmanager
def gateway_test_client(
    application: FastAPI | None = None,
    *,
    home: Path | None = None,
    machine: MachineIdentityReader | None = None,
    config: GatewayConfig | None = None,
    boot: ConfigBoot | None = None,
    fields: Mapping[str, Any] | None = None,
    process: GatewayProcess | None = None,
    cluster_secret: str | None = None,
    session_cookie_secure: bool | None = None,
    catalog: Any = None,
    **kwargs: Any,
) -> Generator[GatewayTestClient]:
    if application is not None:
        if any(
            arg is not None
            for arg in (
                home,
                machine,
                config,
                boot,
                fields,
                process,
                cluster_secret,
                session_cookie_secure,
                catalog,
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
        home=home, machine=machine, config=boot, fields=fields, process=process
    ) as active_process:
        assembled = gateway_test_app(
            process=active_process,
            config=config,
            cluster_secret=cluster_secret,
            session_cookie_secure=session_cookie_secure,
            catalog=catalog,
        )
        with GatewayTestClient(assembled, **kwargs) as client:
            yield client


# meiva:note
# A gateway test client that presents no credential: the module under test is about admission,
# and each test says what it presents.
anonymous_gateway_client = partial(gateway_test_client, credentials=False)


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
