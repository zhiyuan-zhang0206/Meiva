"""Delivery of the active write generation, without a database.

The ledger side is settings-free: the pooler userlist, the launcher's class
grant and the operator's admitted-runtime consumption all read the ledger and
its digest-bound secret. The boot pass keeps a launcher delivery for this home
only, delivers an admitted operator process its gateway login, and records a
refusal that turns a dial of the credential-free endpoint into a named error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from base.db.code_version_gate import ProcessDbGate
from base.db.connections import NoDatabaseAuthorityError, guard_db_url
from base.host.db_authority import delivery, ledger, model
from base.host.env import dotenv_boot

_ENDPOINT = "postgresql://meiva@127.0.0.1:6433/meiva"
_REPO = Path(__file__).resolve().parents[2]


def _encrypt(name: str, _password: str) -> str:
    return f"SCRAM-SHA-256$4096:c2VlZA==${name}"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = (tmp_path / "home").resolve()
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def seeded(home: Path, seed_write_generation: Callable[[Path], Any]) -> Any:
    secret = seed_write_generation(home)
    delivery.ensure_pooler_admin(home, encrypt=_encrypt)
    return secret


def _intent(home: Path, checkout: Path) -> None:
    path = home / "start-intent.json"
    path.write_text(json.dumps({"home": str(home), "checkout": str(checkout)}))
    path.chmod(0o600)


# ── ledger-side delivery ─────────────────────────────────────────────────────


def test_userlist_holds_exactly_the_generation_and_the_admin_console(
    home: Path, seeded: Any
) -> None:
    body = delivery.render_userlist(home, delivery.active_generation(home)).decode()
    admin = delivery.read_pooler_admin(home)
    assert body.splitlines() == [
        f'"{seeded.roles.gateway.name}" "{seeded.roles.gateway.verifier}"',
        f'"{seeded.roles.runner.name}" "{seeded.roles.runner.verifier}"',
        f'"{delivery.POOLER_ADMIN}" "{admin.verifier}"',
    ]
    for password in (seeded.roles.gateway.password, seeded.roles.runner.password, admin.password):
        assert password not in body


def test_pooler_admin_is_created_once_and_never_rotated(home: Path, seeded: Any) -> None:
    del seeded
    first = delivery.read_pooler_admin(home)
    again = delivery.ensure_pooler_admin(home, encrypt=_encrypt)
    assert again == first
    info = delivery.pooler_admin_path(home).stat()
    assert info.st_mode & 0o077 == 0


def test_write_grant_is_the_active_class_login_bound_to_the_ledger(home: Path, seeded: Any) -> None:
    grant = delivery.write_grant(home, "runner")
    assert (grant.role, grant.password) == (seeded.roles.runner.name, seeded.roles.runner.password)
    assert grant.reference == {
        "number": 0,
        "credential_digest": delivery.active_generation(home).credential_digest,
    }
    assert grant.dsn(_ENDPOINT) == (
        f"postgresql://{grant.role}:{grant.password}@127.0.0.1:6433/meiva"
    )


def test_secret_is_delivered_from_store(home: Path, seeded: Any) -> None:
    del seeded
    path = home / "db-authority" / "generations" / "0.json"
    body = json.loads(path.read_text())
    body["roles"]["gateway"]["password"] = "x" * 40
    path.write_text(json.dumps(body))
    grant = delivery.write_grant(home, "gateway")
    assert grant.password == "x" * 40


def test_a_home_without_generation_is_refused(home: Path) -> None:
    groups = model.Groups(gateway=model.GATEWAY_GROUP, runner=model.RUNNER_GROUP)
    ledger.create_ledger(home, owner="meiva", groups=groups)
    with pytest.raises(model.LedgerRefusedError, match="has no generation"):
        delivery.write_grant(home, "gateway")


# ── the boot pass ────────────────────────────────────────────────────────────


# meiva:note
# A unit's home and the environment dict its boot pass runs against.
class Boot(NamedTuple):
    home: Path
    env: dict[str, str]


@pytest.fixture
def boot(home: Path) -> Boot:
    """A home whose `.env` declares the endpoint, and a launch environment that carries it.

    The authority pass force-assigns and DROPS cluster keys in the dict it is given,
    so nothing here touches the test process's own environment."""
    (home / ".env").write_text(f"MEIVA_DB_URL={_ENDPOINT}\n")
    return Boot(home, {"MEIVA_DB_URL": _ENDPOINT})


def test_a_delivered_login_for_this_home_survives_the_env_file(boot: Boot) -> None:
    delivered = "postgresql://meiva_g4_gateway:delivered@127.0.0.1:6433/meiva"
    boot.env.update(
        {
            "MEIVA_PROCESS_PROFILE": "gateway",
            "MEIVA_DB_URL": delivered,
            delivery.GENERATION_ENV: "4",
        }
    )
    result = dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    assert boot.env["MEIVA_DB_URL"] == delivered
    assert result.db_authority_refusal is None


def test_a_sibling_homes_delivery_is_replaced_by_this_homes_endpoint(boot: Boot) -> None:
    boot.env.update(
        {
            "MEIVA_PROCESS_PROFILE": "gateway",
            "MEIVA_DB_URL": "postgresql://meiva_g4_gateway:sibling@127.0.0.1:7433/meiva",
            delivery.GENERATION_ENV: "4",
        }
    )
    dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    assert boot.env["MEIVA_DB_URL"] == _ENDPOINT


def test_an_admitted_operator_process_receives_the_gateway_login(boot: Boot, seeded: Any) -> None:
    _intent(boot.home, _REPO)
    result = dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    gateway = seeded.roles.gateway
    assert boot.env["MEIVA_DB_URL"] == (
        f"postgresql://{gateway.name}:{gateway.password}@127.0.0.1:6433/meiva"
    )
    assert boot.env[delivery.GENERATION_ENV] == "0"
    assert result.db_authority_refusal is None


def test_a_foreign_runtime_receives_gateway_login(boot: Boot, seeded: Any, tmp_path: Path) -> None:
    _intent(boot.home, tmp_path / "another-checkout")
    result = dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    gateway = seeded.roles.gateway
    assert boot.env["MEIVA_DB_URL"] == (
        f"postgresql://{gateway.name}:{gateway.password}@127.0.0.1:6433/meiva"
    )
    assert boot.env[delivery.GENERATION_ENV] == "0"
    assert result.db_authority_refusal is None


def test_a_launched_process_without_delivery_is_refused(boot: Boot, seeded: Any) -> None:
    del seeded
    _intent(boot.home, _REPO)
    boot.env["MEIVA_PROCESS_PROFILE"] = "runner"
    result = dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    assert boot.env["MEIVA_DB_URL"] == _ENDPOINT
    refusal = result.db_authority_refusal
    assert refusal is not None and "only the root launcher delivers" in refusal


def test_a_home_without_a_ledger_is_untouched(boot: Boot) -> None:
    result = dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    assert boot.env["MEIVA_DB_URL"] == _ENDPOINT
    assert result.db_authority_refusal is None
    assert guard_db_url(_ENDPOINT, refusal=result.db_authority_refusal) == _ENDPOINT


# ── a URL's own startup options ──────────────────────────────────────────────

_WITH_OPTIONS = (
    "postgresql://osuser@/meiva?host=/tmp/meiva-pg-x&port=5433&options=-c%20role%3Dmeiva_gateway"
)


def test_a_urls_own_startup_options_survive_the_statement_ceiling() -> None:
    from base.db.connections import PG_STATEMENT_TIMEOUT_KWARGS, statement_kwargs

    assert statement_kwargs(_ENDPOINT) is PG_STATEMENT_TIMEOUT_KWARGS
    assert statement_kwargs(_WITH_OPTIONS)["options"] == (
        "-c role=meiva_gateway -c statement_timeout=60000"
    )
    assert statement_kwargs(_WITH_OPTIONS)["connect_timeout"] == 5


def test_a_database_handle_retains_its_boot_refusal_after_another_delivery(
    boot: Boot,
    seeded: Any,
    tmp_path: Path,
    database_gate: ProcessDbGate,
) -> None:
    """A later admitted boot cannot authorize a previously refused handle."""
    del seeded, tmp_path
    from base.config import Settings
    from base.db import Database
    from base.db import config as db_config

    boot.env["MEIVA_PROCESS_PROFILE"] = "runner"
    result = dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)
    first = Settings(
        env_boot=result, data_plane={"db_url": _ENDPOINT, "redis_url": "redis://localhost:1"}
    )
    handle = Database(
        db_config.DbConfig(
            db_url=first.data_plane.db_url,
            db_sslmode=first.data_plane.db_sslmode,
            db_pool_min_size=first.data_plane.db_pool_min_size,
            db_pool_max_size=first.data_plane.db_pool_max_size,
            pgbouncer_enabled=first.data_plane.pgbouncer_enabled,
            db_authority_refusal=first.env_boot.db_authority_refusal,
        ),
        gate=database_gate,
    )
    del boot.env["MEIVA_PROCESS_PROFILE"]
    dotenv_boot.enforce_cluster_env_authority(boot.env, boot.home)

    with pytest.raises(NoDatabaseAuthorityError, match="only the root launcher delivers"):
        handle.connect()
