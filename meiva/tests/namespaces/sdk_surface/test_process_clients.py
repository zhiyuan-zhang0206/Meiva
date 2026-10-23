import sys
from pathlib import Path

import pytest
import redis

from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.proc import run_bounded
from meiva.boot import process_clients

_CHILD = Path(__file__).with_name("child_gateway_credentials.py")


@pytest.fixture
def config_boot() -> ConfigBoot:
    return snapshot_process_config()


# meiva:note
# An empty database URL is refused by the process clients' own factory, before any connection
# can be dialled: the failure is the SDK's RuntimeError, not libpq's connection error that an
# empty DSN would produce if it reached the driver.
def test_default_sql_refuses_a_missing_resource_before_dial(config_boot: ConfigBoot) -> None:
    clients = process_clients(config=config_boot)
    config_boot.set_field("db_url", "")
    try:
        with pytest.raises(RuntimeError, match="MEIVA_DB_URL not set"):
            clients.sql.execute("SELECT 1")
    finally:
        clients.close()


def test_process_custom_database_is_independent_of_the_default(
    db_url: str,
    config_boot: ConfigBoot,
    database_gate: ProcessDbGate,
) -> None:
    database = Database.from_settings(gate=database_gate)
    custom = process_clients(database=lambda: database, config=config_boot)
    default = process_clients(config=config_boot)
    config_boot.set_field("db_url", "")
    try:
        assert custom.sql.execute("SELECT 44").fetchone() == (44,)
        with pytest.raises(RuntimeError, match="MEIVA_DB_URL not set"):
            default.sql.execute("SELECT 1")
    finally:
        custom.close()
        default.close()


def test_process_resources_resolve_after_overlay_and_again_after_close(
    db_url: str,
    redis_url: str,
    config_boot: ConfigBoot,
) -> None:
    config_boot.set_field("db_url", "")
    config_boot.set_field("redis_url", "")
    clients = process_clients(config=config_boot)
    config_boot.set_field("db_url", db_url)
    config_boot.set_field("redis_url", redis_url)
    try:
        assert clients.sql.execute("SELECT 43").fetchone() == (43,)
        assert clients.redis.set("process-clients-overlay", "ready")
        assert clients.redis.get("process-clients-overlay") == "ready"
        native = clients.redis._get()
        assert type(native) is redis.Redis
        assert native.connection_pool.connection_kwargs["socket_timeout"] == 10.0
        clients.close()
        config_boot.set_field("db_url", "")
        config_boot.set_field("redis_url", "")
        with pytest.raises(RuntimeError, match="MEIVA_DB_URL not set"):
            clients.sql.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="MEIVA_REDIS_URL not set"):
            clients.redis.ping()
    finally:
        clients.close()


# meiva:note
# The child's gateway client reads the endpoint the description carries and the credential the
# environment holds at the moment of first use, and again after its clients close. The
# environment is the child's own, so the scenario runs in a real interpreter that changes it.
def test_child_uses_description_endpoint_and_credentials_at_first_use() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_process_boot_builds_no_connection_stack(pytester: pytest.Pytester) -> None:
    code = (
        "import sys, importlib, threading\n"
        "import meiva\n"
        "image = meiva._LOADED_IMAGE\n"
        "assert 'base.db' not in sys.modules\n"
        "assert 'meiva.sdk_surface.extensions' not in sys.modules\n"
        "assert 'base.packages.plugins.loading.registry' not in sys.modules\n"
        "assert not getattr(meiva, '__plugin_installation__', None)\n"
        "assert not [t for t in threading.enumerate() if t is not threading.main_thread()]\n"
        "importlib.reload(meiva)\n"
        "assert meiva._LOADED_IMAGE is image\n"
        "assert 'base.db' not in sys.modules\n"
        "from meiva.boot import process_clients, context_from_description\n"
        "before = set(sys.modules)\n"
        "from base.config import ConfigBoot\n"
        "owner = ConfigBoot()\n"
        "process_clients(config=owner)\n"
        "context_from_description({'identity': {'agent_id': 17, 'owns_loop': True, "
        "'actor': None}, 'gateway_url': 'http://gateway.test'}, config=owner)\n"
        "print(sorted(m for m in ('psycopg', 'psycopg_pool', 'redis', 'httpx', 'langchain_core') "
        "if m in sys.modules and m not in before))\n"
    )
    proc = pytester.run(sys.executable, "-I", "-c", code, timeout=120)
    assert proc.ret == 0, proc.stderr.str()
    proc.stdout.fnmatch_lines(["[]"])


# meiva:note
# Process clients built over an unprepared configuration owner read nothing at construction:
# the owner captures the environment on the first configuration read, once, without delivering
# or mutating it, and a closed set rebuilds from the same capture.
def test_clients_over_an_unprepared_owner_capture_only_on_first_read(
    pytester: pytest.Pytester,
) -> None:
    code = """
import os
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from meiva.boot import process_clients
from base.config import ConfigBoot

owner = ConfigBoot()
before = dict(os.environ)
clients = process_clients(config=owner)
inputs = clients.get(GatewayTransportInputs)
assert not owner.prepared
assert inputs.max_retries_reader() > 0
assert owner.prepared
captured = owner.environment
assert inputs.retry_delay_reader() >= 0
assert inputs.memory_deadline_reader() > 0
assert owner.environment is captured
assert dict(os.environ) == before
clients.close()
assert clients.get(GatewayTransportInputs).max_retries_reader() > 0
assert owner.environment is captured
assert dict(os.environ) == before
clients.close()
"""
    result = pytester.run(sys.executable, "-I", "-c", code, timeout=30)
    assert result.ret == 0, result.stderr.str()


def test_gateway_clients_keep_independent_live_config_and_rebuild_credentials(
    config_boot: ConfigBoot,
) -> None:
    second = snapshot_process_config()
    config_boot.set_field("gateway_url", "http://first.test/")
    second.set_field("gateway_url", "http://second.test/")
    config_boot.set_field("cluster_secret", "first-secret")
    second.set_field("cluster_secret", "second-secret")
    first_clients = process_clients(config=config_boot)
    second_clients = process_clients(config=second)
    try:
        first_gateway = first_clients.gateway
        second_gateway = second_clients.gateway
        assert str(first_gateway.base_url).rstrip("/") == "http://first.test"
        assert str(second_gateway.base_url).rstrip("/") == "http://second.test"
        assert first_gateway.headers["Authorization"] == "Bearer first-secret"
        assert second_gateway.headers["Authorization"] == "Bearer second-secret"
        first_clients.close()
        config_boot.set_field("cluster_secret", "first-updated")
        assert first_clients.gateway.headers["Authorization"] == "Bearer first-updated"
        assert second_clients.gateway is second_gateway
        assert second_gateway.headers["Authorization"] == "Bearer second-secret"
    finally:
        first_clients.close()
        second_clients.close()
