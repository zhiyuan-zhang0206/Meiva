from __future__ import annotations

import ast
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base import paths
from base.agents.context.clients import ClientSet
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.host import proc
from base.host.net.auth import bearer_header, cookie_name
from base.host.system.tests.fake_commands import fake_host
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from base.telemetry import DrainStatus
from gateway import app as gateway_app
from gateway.cluster import server
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.model_catalog import load_model_catalog
from gateway.tests.auth.gateway_config import (
    gateway_config_of,
    gateway_test_app,
    gateway_test_client,
)

_CHILD = Path(__file__).with_name("child_gateway_scenarios.py")


def test_both_gateway_entries_use_the_same_first_load_fact() -> None:
    assert server.LOADED_IMAGE is LOADED_IMAGE
    assert gateway_app.LOADED_IMAGE is LOADED_IMAGE


# meiva:note
# The boot state of a configuration owner nothing has read a value from. A read of any field
# prepares the owner (a lite field) or upgrades it to the eager chain (every other field), so an
# owner still unprepared, at its initial mode and without a pending overlay was never asked.
_NEVER_READ = {
    "mode": "lite",
    "reason": None,
    "upgrades": 0,
    "pending_count": 0,
    "prepared": False,
}


def test_gateway_owner_construction_is_cold(*, machine_identity: MachineIdentityReader) -> None:
    config = ConfigBoot()
    process = GatewayProcess(
        home=meiva_home(),
        config=config,
        image=LoadedCommit(Path("unknown-source"), None),
        machine=machine_identity,
        host=fake_host(),
    )
    with process.lifetime():
        assert process.gate.min_read_due()
        assert process.clients.sync_events().status is DrainStatus.COMPLETED
    assert config.boot_state() == _NEVER_READ


# meiva:note
# Every handle the process builds names the connections it opens after the entry's image.
def test_gateway_handles_retain_the_same_entry_gate(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    process = GatewayProcess(
        home=meiva_home(),
        config=snapshot_process_config(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    db_conn.commit()
    with process.lifetime():
        first = process.database()
        second = process.database()
        assert first is not second
        names: list[str] = []
        for handle in (first, second):
            with handle.connect() as conn:
                row = conn.execute("SHOW application_name").fetchone()
                assert row is not None
                names.append(row[0])
    assert names == [f"meiva:gateway:v{process.version.get()}"] * 2


# meiva:note
# An event writer whose stop fails, as one that cannot flush its last batch would.
class _FailsToStop:
    def stop(self, timeout: float) -> object:
        raise RuntimeError("writer close failed")


def test_gateway_cleanup_preserves_a_startup_primary(
    *, machine_identity: MachineIdentityReader
) -> None:
    primary = ValueError("startup failed")
    process = GatewayProcess(
        home=meiva_home(),
        config=ConfigBoot(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    process.clients = ClientSet(pipeline_factory=_FailsToStop)
    process.clients.event_pipeline()
    with pytest.raises(ValueError) as caught, process.lifetime():
        raise primary
    assert caught.value is primary
    assert any("writer close failed" in note for note in primary.__notes__)


def test_gateway_first_load_image_does_not_follow_a_checkout_move(tmp_path: Path) -> None:
    source = tmp_path / "source"
    module = source / "gateway" / "cluster"
    module.mkdir(parents=True)
    (source / "gateway" / "__init__.py").write_text("")
    (module / "__init__.py").write_text("")
    original = Path(server.__file__).with_name("process_boot.py")
    (module / "process_boot.py").write_text(original.read_text())
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=entry-proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "first",
        ],
    ):
        result = proc.run_bounded(
            ["git", "-C", str(source), *args], timeout=10, capture_output=True
        )
        assert result.returncode == 0, result.stderr
    env = dict(os.environ)
    env["MEIVA_HOME"] = str(tmp_path / "home")
    env["MEIVA_CONFIG_FETCH"] = "skip"
    result = proc.run_bounded(
        [
            sys.executable,
            str(Path(__file__).with_name("child_gateway_scenarios.py")),
            "checkout_move",
            str(source),
            str(Path(__file__).resolve().parents[3]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    fact = json.loads(result.stdout)
    assert fact["same"] is True
    assert fact["first"] == fact["after"]
    assert fact["first"] != fact["current"]
    assert fact["version"] == 1


# meiva:note
# The health route reports the commit the entry captured, not the checkout's HEAD.
def test_asgi_health_keeps_its_explicit_entry_image(
    tmp_path: Path, database: Database, *, machine_identity: MachineIdentityReader
) -> None:
    # meiva:note
    # the throwaway data plane the process dials
    _ = database
    source = tmp_path / "entry-source"
    source.mkdir()
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=entry-proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "entry",
            "--allow-empty",
        ],
    ):
        result = proc.run_bounded(
            ["git", "-C", str(source), *args], timeout=10, capture_output=True
        )
        assert result.returncode == 0, result.stderr
    image = LoadedCommit.capture(source)
    assert image.sha is not None
    assert image.sha != LOADED_IMAGE.sha
    process = GatewayProcess(
        home=meiva_home(),
        config=snapshot_process_config(),
        image=image,
        machine=machine_identity,
        host=fake_host(),
    )
    app = gateway_test_app(process=process)
    with (
        process.lifetime(),
        gateway_test_client(app) as client,
    ):
        first = client.get("/api/health")
        second = client.get("/api/health")
        assert first.status_code == second.status_code == 200
        assert first.json()["sha"] == second.json()["sha"] == image.sha
        assert app.state.gateway_process is process
        assert app.state.process_image is image


def test_openapi_schema_is_cold() -> None:
    config = ConfigBoot()
    schema = gateway_app.openapi_schema_of(config, home=meiva_home())
    assert isinstance(schema, dict)
    assert "/api/health" in schema["paths"]
    assert config.boot_state() == _NEVER_READ


def test_the_memory_checkout_follows_the_home_the_entry_hands_in(tmp_path: Path) -> None:
    home = tmp_path / "served-home"

    memory_root = gateway_app.process_config(snapshot_process_config(), home=home).memory_root

    assert memory_root in {home / "memory", home / "gateway" / "memory"}
    assert home.is_dir()


# meiva:note
# A reload worker builds its app from its own environment: booting loads the unit's
# `.env` into the process, so the check runs in a process of its own.
def test_create_app_from_environment_assembles_gateway_config(
    config_authority: ConfigAuthority,
) -> None:
    config_authority.env_path.write_text(
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        f"MEIVA_DB_URL={config_authority.service_field_value('db_url')}\n"
        f"MEIVA_REDIS_URL={config_authority.service_field_value('redis_url')}\n",
        encoding="utf-8",
    )
    home = config_authority.env_path.parent
    result = proc.run_bounded(
        [sys.executable, str(_CHILD), "app_from_environment", str(home)],
        env={**os.environ, "MEIVA_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "distinct_apps": True,
        "config_matches_boot": True,
        "acceptance_empty": True,
        "acceptance_independent": True,
    }


_ROOT_READS = frozenset({"meiva_home", "resolve_meiva_home"})


def _rooted_app(home: Path) -> Any:
    boot = snapshot_process_config()
    process = GatewayProcess(
        home=home,
        config=boot,
        image=LOADED_IMAGE,
        machine=ConfiguredMachineIdentity(boot),
        host=fake_host(),
    )
    return gateway_app.create_app(
        gateway_config_of(process.config),
        process=process,
        process_ownership="owned",
        catalog=load_model_catalog(build_model_catalog),
    )


def _second_root(first: Path) -> Path:
    home = first / "second-root"
    home.mkdir(mode=0o700)
    return home


def test_a_second_root_reads_none_of_the_first_roots_credentials_or_session(
    config_authority: ConfigAuthority, seed_write_generation: Callable[[Path], Any]
) -> None:
    first = config_authority.env_path.parent
    assert paths.meiva_home() == first
    second = _second_root(first)
    tokens = seed_write_generation(first).api

    with gateway_test_client(_rooted_app(first)) as client:
        assert client.get("/api/agents", headers=bearer_header(tokens.runner)).status_code == 200
        cookie = client.post("/api/auth/login", json={"password": tokens.runner}).cookies[
            cookie_name()
        ]
        assert cookie

    presented = {"Cookie": f"{cookie_name()}={cookie}"}
    with gateway_test_client(_rooted_app(second)) as other:
        assert other.get("/api/agents", headers=bearer_header(tokens.runner)).status_code == 401
        assert other.get("/api/auth/check", headers=presented).json() == {"authenticated": False}
        assert other.get("/api/agents").status_code == 200


def test_an_app_serves_the_config_file_of_the_root_it_serves(
    config_authority: ConfigAuthority,
) -> None:
    first = config_authority.env_path.parent
    second = _second_root(first)
    (first / ".env").write_text(
        "MEIVA_DB_URL=postgresql://meiva_first@10.9.9.1:5432/meiva\n", encoding="utf-8"
    )
    (second / ".env").write_text(
        "MEIVA_DB_URL=postgresql://meiva_second@10.9.9.2:5432/meiva\n", encoding="utf-8"
    )

    with gateway_test_client(_rooted_app(first)) as client:
        assert "10.9.9.1" in client.get("/api/bootstrap").json()["MEIVA_DB_URL"]
    with gateway_test_client(_rooted_app(second)) as other:
        assert "10.9.9.2" in other.get("/api/bootstrap").json()["MEIVA_DB_URL"]


def test_serving_an_app_adds_no_environment_slot(config_authority: ConfigAuthority) -> None:
    second = _second_root(config_authority.env_path.parent)
    before = set(os.environ)
    with gateway_test_client(_rooted_app(second)) as client:
        assert client.get("/api/health").status_code == 200
    assert set(os.environ) == before


def test_health_reports_the_serving_home_verbatim_for_the_probe(tmp_path: Path) -> None:
    roots = tmp_path / "roots"
    roots.mkdir()
    alias = tmp_path / "roots-alias"
    alias.symlink_to(roots, target_is_directory=True)
    served = alias / "home"
    served.mkdir(mode=0o700)

    with gateway_test_client(_rooted_app(served)) as redirected:
        verbatim = redirected.get("/api/health").json()["home"]
    with gateway_test_client() as unit:
        assert unit.get("/api/health").json()["home"] == str(paths.meiva_home())

    assert verbatim == str(served)
    assert verbatim != str(served.resolve())


def _root_read_calls(node: ast.AST) -> list[ast.Call]:
    return [
        inner
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and _names_root_read(inner.func)
    ]


def _names_root_read(func: ast.expr) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _ROOT_READS


def _resolving_functions(tree: ast.Module) -> dict[str, int]:
    per_function: dict[str, int] = {}

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = _root_read_calls(child)
                if calls:
                    per_function[child.name] = len(calls)
                walk(child)
                continue
            walk(child)

    walk(tree)
    return per_function


def test_the_data_root_is_resolved_only_at_the_gateway_entries() -> None:
    package = Path(paths.repo_root()) / "gateway"
    found = {
        str(path.relative_to(Path(paths.repo_root()))): _resolving_functions(
            ast.parse(path.read_text(encoding="utf-8"))
        )
        for path in sorted(package.rglob("*.py"))
        if "tests" not in path.relative_to(package).parts
        and "__pycache__" not in path.parts
        and not path.name.startswith("test_")
    }
    assert {module: functions for module, functions in found.items() if functions} == {
        "gateway/app.py": {"create_app_from_environment": 1, "main": 1, "openapi_schema": 1},
    }
