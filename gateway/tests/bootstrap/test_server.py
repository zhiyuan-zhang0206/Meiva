from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.net.auth import ClusterSecretMissingError
from gateway.cluster import server as _server


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


def _config_with(fields: dict[str, object]) -> ConfigBoot:
    config = snapshot_process_config()
    for name, value in fields.items():
        config.set_field(name, value)
    return config


def test_gateway_pins_uvicorn_to_one_worker_for_process_local_rate_limits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, object] = {}

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return

    def _record_serve(kwargs: dict[str, object]) -> None:
        captured.update(kwargs)

    with caplog.at_level(logging.WARNING, logger="gateway.cluster.server"):
        _server.main(
            build_app=lambda _process: _app(),
            config_factory=lambda: _config_with(
                {
                    "gateway_pidfile": tmp_path / "gateway.pid",
                    "machine_serve_gateway": False,
                }
            ),
            schema_checker=_ignore,
            fd_limit_raiser=_ignore,
            process_logger_initializer=_ignore,
            faulthandler_registrar=_ignore,
            server_runner=_record_serve,
        )

    assert captured["workers"] == 1
    assert "rate limit" in caplog.text


def test_gateway_launch_bounds_the_connection_drain() -> None:
    kwargs = _server.serve_kwargs(
        host="127.0.0.1",
        app=_app(),
        factory=False,
        config=_config_with({"gateway_graceful_shutdown_timeout_seconds": 7.5}),
    )
    assert kwargs["timeout_graceful_shutdown"] == 7.5


@pytest.mark.parametrize(("serves_gateway", "raised"), [(True, 1), (False, 0)])
def test_gateway_start_raises_the_min_code_version_after_schema_and_logging(
    tmp_path: Path, serves_gateway: bool, raised: int
) -> None:
    steps: list[str] = []

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return

    def _schema_asserted(_url: str) -> None:
        steps.append("schema")

    def _raised(_gate: ProcessDbGate, _db: Database) -> int:
        steps.append("raise")
        return 1

    def logging_initialized(**_inputs: object) -> None:
        steps.append("logging")

    _server.main(
        build_app=lambda _process: _app(),
        config_factory=lambda: _config_with(
            {
                "gateway_pidfile": tmp_path / "gateway.pid",
                "machine_serve_gateway": serves_gateway,
            }
        ),
        schema_checker=_schema_asserted,
        fd_limit_raiser=_ignore,
        process_logger_initializer=logging_initialized,
        faulthandler_registrar=_ignore,
        server_runner=_ignore,
        code_version_raiser=_raised,
    )

    assert steps.count("raise") == raised
    if raised:
        assert steps == ["schema", "logging", "raise"]


def _run_process_entry(
    tmp_path: Path,
    *,
    gateway: bool,
    reload: bool = False,
    cluster_value: str | None = None,
    machine_host: str | None = None,
    captured: list[dict[str, object]] | None = None,
    build_app: Any = None,
) -> list[str]:
    steps: list[str] = []

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return

    def _config_boot() -> ConfigBoot:
        fields: dict[str, object] = {
            "gateway_pidfile": tmp_path / "gateway.pid",
            "machine_serve_gateway": gateway,
            "gateway_reload": reload,
        }
        if cluster_value is not None:
            fields["cluster_secret"] = cluster_value
        if machine_host is not None:
            fields["machine_host"] = machine_host
        return _config_with(fields)

    def _schema(_url: str) -> None:
        steps.append("schema")

    def _serve(kwargs: dict[str, object]) -> None:
        if captured is not None:
            captured.append(dict(kwargs))
        steps.append(f"serve:{kwargs['host']}")

    _server.main(
        build_app=(lambda _process: _app()) if build_app is None else build_app,
        config_factory=_config_boot,
        schema_checker=_schema,
        fd_limit_raiser=_ignore,
        process_logger_initializer=_ignore,
        faulthandler_registrar=_ignore,
        server_runner=_serve,
        code_version_raiser=_ignore,
    )
    return steps


def test_a_gateway_without_a_cluster_secret_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ClusterSecretMissingError, match="MEIVA_CLUSTER_SECRET"):
        _run_process_entry(tmp_path, gateway=True, cluster_value="")

    assert not (tmp_path / "gateway.pid").exists()


@pytest.mark.parametrize(
    ("reachable", "gateway", "bind"),
    [
        ("localhost", True, "127.0.0.1"),
        ("10.0.0.5", True, ""),
        ("10.0.0.5", False, "127.0.0.1"),
    ],
)
def test_gateway_bind_follows_the_declared_reachable_address_not_the_secret(
    tmp_path: Path,
    reachable: str,
    gateway: bool,
    bind: str,
) -> None:
    steps = _run_process_entry(
        tmp_path,
        gateway=gateway,
        cluster_value="s3cret",
        machine_host=reachable,
    )

    assert steps[-1] == f"serve:{bind}"


@pytest.mark.parametrize("reload_enabled", [False, True])
def test_main_and_serve_kwargs_distinguish_reload_factory_from_single_worker(
    tmp_path: Path,
    reload_enabled: bool,
) -> None:
    captured_list: list[dict[str, object]] = []
    built_processes: list[object] = []
    app_instance = _app()

    def _build_app(process: object) -> Any:
        built_processes.append(process)
        return app_instance

    _run_process_entry(
        tmp_path,
        gateway=False,
        reload=reload_enabled,
        captured=captured_list,
        build_app=_build_app,
    )
    assert len(captured_list) == 1
    captured = captured_list[0]
    subset = {key: captured[key] for key in ("app", "factory", "reload", "reload_dirs", "workers")}
    if reload_enabled:
        assert (built_processes, subset) == (
            [],
            {
                "app": "gateway.app:create_app_from_environment",
                "factory": True,
                "reload": True,
                "reload_dirs": ["gateway", "base", "meiva", "agent"],
                "workers": 1,
            },
        )
    else:
        assert (len(built_processes), subset) == (
            1,
            {
                "app": app_instance,
                "factory": False,
                "reload": False,
                "reload_dirs": None,
                "workers": 1,
            },
        )
