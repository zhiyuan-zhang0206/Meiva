"""`meiva init` records one home identity before effects; `meiva start` admits it."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from base import cluster
from base.deploy.lifecycle import start_serving
from base.host.net.tests.local_http import closed_port_url
from cli import init_intent, start_intent
from cli import start_identity as identity
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.lifecycle import root_driver
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.start_runtime import HomeSite
from cli.startup.service_path import CallerHost
from cli.tests.bootstrap.init_identity import (
    break_env_publication,
    caller_host,
    prepare_init_identity,
    repair_env_publication,
)

_ADMITTED_HOME = Path(__file__).with_name("child_admitted_home.py")


@pytest.fixture(autouse=True)
def _isolate_start_environment() -> Iterator[None]:
    """Real start clears derived keys; that process-local effect ends with its test."""
    with patch.dict(os.environ):
        yield


@pytest.fixture
def inputs(tmp_path: Path) -> identity.IdentityInput:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return identity.IdentityInput(
        tmp_path / "home",
        checkout,
        frozenset({"gateway", "agent-runner"}),
        {"MEIVA_MACHINE_NAME": "preview"},
        occupied_ports=frozenset(),
    )


@pytest.fixture
def site(inputs: identity.IdentityInput) -> HomeSite:
    return HomeSite(inputs.home, inputs.checkout)


@pytest.fixture
def caller(tmp_path: Path) -> CallerHost:
    """A caller whose PATH is one tool directory and who declares nothing else."""
    return caller_host(path=str(tmp_path / "tools"))


def test_fresh_identity_committed_before_native_work(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    data = identity.read_intent(inputs.home)
    assert data is not None and data["phase"] == "configured"
    rec = cluster.get_record(inputs.home)
    env = dotenv_values(inputs.home / ".env")
    assert rec is not None and "pgbouncer" in rec.ports
    db_url = env["MEIVA_DB_URL"]
    assert db_url is not None and db_url.endswith(f":{rec.ports['pgbouncer']}/meiva")
    # The database endpoint is credential-free: the owner is NOLOGIN and the
    # first start mints write generation 0; no DB password is ever minted here.
    assert urlsplit(db_url).password is None
    # Every gateway home is born with a human secret, a single box included.
    assert env["MEIVA_CLUSTER_SECRET"]
    assert "MEIVA_DB_ADMIN_PASSWORD" not in env
    assert "MEIVA_RUNNER_DB_PASSWORD" not in env
    # Redis always authenticates: a single box is born with
    # independent Redis admin and runtime passwords, the runtime one in its URL.
    redis_admin, redis_runtime = env["MEIVA_REDIS_ADMIN_PASSWORD"], env["MEIVA_REDIS_PASSWORD"]
    assert redis_admin and redis_runtime and redis_admin != redis_runtime
    redis_url = urlsplit(env["MEIVA_REDIS_URL"] or "")
    assert (redis_url.username, redis_url.password) == ("meiva", redis_runtime)
    assert not (inputs.home / "pg").exists()


def test_an_initialized_home_is_refused_again_and_keeps_its_bytes(
    inputs: identity.IdentityInput,
) -> None:
    inputs = replace(inputs, roles=frozenset({"gateway"}))
    identity.prepare_identity(inputs)
    paths = [inputs.home / ".env", inputs.home / identity.INTENT_NAME]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    env = dotenv_values(paths[0])
    keys = ("MEIVA_CLUSTER_SECRET", "MEIVA_REDIS_ADMIN_PASSWORD", "MEIVA_REDIS_PASSWORD")
    assert len({env[k] for k in keys}) == len(keys)
    assert all(env[k] for k in keys)
    for phase in ("configured", "provisioned", "ready"):
        identity.mark_phase(inputs.home, phase)
        with pytest.raises(RuntimeError, match=rf"already initialized \(phase {phase}\)"):
            identity.prepare_identity(inputs)
        with pytest.raises(RuntimeError, match="already initialized"):
            identity.resume_claim(inputs.home)
        if phase == "configured":
            assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before
    assert dotenv_values(paths[0]) == env


def test_a_gateway_birth_pins_a_minted_logical_backup_passphrase(
    inputs: identity.IdentityInput,
) -> None:
    """Logical backups are encrypted under a passphrase the birth mints and pins
    (0600), independent of the cluster secret: a random one, never the public
    sha256(""). An interrupted birth keeps it."""
    from services.backup.artifact import passphrase

    break_env_publication(inputs.home)
    with pytest.raises(OSError):
        identity.prepare_identity(inputs)
    minted = passphrase.pinned(inputs.home)
    assert minted is not None and minted != passphrase.derive("")
    assert passphrase.pin_path(inputs.home).stat().st_mode & 0o777 == 0o600
    repair_env_publication(inputs.home)
    identity.resume_claim(inputs.home)
    assert dotenv_values(inputs.home / ".env")["MEIVA_CLUSTER_SECRET"]
    assert passphrase.resolve(inputs.home) == minted


def test_a_remote_unit_birth_pins_no_backup_passphrase(inputs: identity.IdentityInput) -> None:
    """Only a gateway runs logical backups; a joining agent-runner holds no key."""
    from services.backup.artifact import passphrase

    runner = replace(
        inputs,
        roles=frozenset({"agent-runner"}),
        values={**inputs.values, "MEIVA_GATEWAY_URL": "http://10.0.0.7:8000"},
    )
    identity.prepare_identity(runner)
    assert passphrase.pinned(runner.home) is None


def test_a_published_claim_keeps_no_copy_of_its_credentials(
    inputs: identity.IdentityInput,
) -> None:
    """The intent carries the birth's `.env` payload only while it is claiming,
    so an interrupted birth resumes the same credentials. Once `.env` holds
    them the intent drops the payload: no stale copy of the human secret or the
    Redis passwords outlives a rotation there."""
    inputs = replace(inputs, roles=frozenset({"gateway"}))
    identity.prepare_identity(inputs)
    env = dotenv_values(inputs.home / ".env")
    credentials = [
        env[key]
        for key in ("MEIVA_CLUSTER_SECRET", "MEIVA_REDIS_ADMIN_PASSWORD", "MEIVA_REDIS_PASSWORD")
    ]
    assert all(credentials)
    data = identity.read_intent(inputs.home)
    assert data is not None and data["phase"] == "configured" and data["env"] == {}
    raw = (inputs.home / identity.INTENT_NAME).read_text()
    assert not [value for value in credentials if value and value in raw]


def test_birth_writes_nothing_outside_the_home(inputs: identity.IdentityInput) -> None:
    """A home describes only itself: no host-level file and no file in the
    checkout lists the cluster."""
    identity.prepare_identity(inputs)
    assert sorted(p.name for p in inputs.home.parent.iterdir()) == ["checkout", "home"]
    assert list(inputs.checkout.iterdir()) == []


def test_crash_after_intent_before_env_resumes_same_claim(inputs: identity.IdentityInput) -> None:
    break_env_publication(inputs.home)
    with pytest.raises(OSError):
        identity.prepare_identity(inputs)
    pending = identity.read_intent(inputs.home)
    assert pending is not None and pending["phase"] == "claiming"
    repair_env_publication(inputs.home)
    identity.resume_claim(inputs.home)
    assert cluster.get_record(inputs.home) == cluster.ClusterRecord(**pending["record"])
    assert dict(dotenv_values(inputs.home / ".env")) == pending["env"]


def test_an_intent_carrying_the_retired_worktree_key_is_refused_by_name(
    inputs: identity.IdentityInput,
) -> None:
    """The `worktree` flag an older start recorded is not read, migrated or
    ignored: a home whose intent still carries it fails loudly at its next start,
    naming the key, until the operator removes it."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["worktree"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match=r"unexpected keys \[.*'worktree'"):
        identity.read_intent(inputs.home)


# Historical identity-key retirement procedure.
_STRIP_RETIRED_INTENT_KEY = (
    "python3 -c 'import json,os,sys;p=sys.argv[1];d=json.load(open(p));"
    'd.pop("worktree",None);t=p+".tmp";'
    "f=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.fchmod(f,0o600);"
    'os.write(f,(json.dumps(d,sort_keys=True)+"\\n").encode());os.fsync(f);os.close(f);'
    "os.replace(t,p)' "
)


def test_the_rollout_step_makes_an_old_intent_readable_idempotently(
    inputs: identity.IdentityInput,
) -> None:
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    born_before = json.loads(path.read_text()) | {"worktree": True}
    path.write_text(json.dumps(born_before, sort_keys=True) + "\n")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="unexpected keys"):
        identity.read_intent(inputs.home)

    command = _STRIP_RETIRED_INTENT_KEY + f"'{path}'"
    for _ in range(2):  # the second run changes nothing
        subprocess.run(command, shell=True, check=True, capture_output=True)  # noqa: S602 — the quoted rollout line
        assert path.stat().st_mode & 0o777 == 0o600
        assert not path.with_name(path.name + ".tmp").exists()
        data = identity.read_intent(inputs.home)
        assert data == {k: v for k, v in born_before.items() if k != "worktree"}


def test_configured_home_missing_its_intent_is_not_reborn(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    (inputs.home / identity.INTENT_NAME).unlink()
    with pytest.raises(RuntimeError, match="reattachment"):
        identity.prepare_identity(inputs)
    assert identity.read_intent(inputs.home) is None


def test_existing_data_without_intent_refuses(inputs: identity.IdentityInput) -> None:
    (inputs.home / "pg").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no initialization authority"):
        identity.prepare_identity(inputs)
    assert identity.read_intent(inputs.home) is None


def _args(*extra: str):
    from cli.parsers import build_parser

    return build_parser().parse_args(["init", *extra])


def _single_box(*extra: str):
    """An init of a single-box home: gateway and agent-runner, named."""
    return _args("--serve-gateway", "--serve-agent-runner", "--machine-name", "preview", *extra)


def test_init_uses_explicit_home_not_ambient_projection(
    inputs: identity.IdentityInput,
    site: HomeSite,
    caller: CallerHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEIVA_DB_URL", "postgresql://foreign@foreign.invalid/production")
    monkeypatch.setenv("MEIVA_CLUSTER_SECRET", "foreign-secret")
    prepare_init_identity(_single_box(), site, caller)
    env = dotenv_values(inputs.home / ".env")
    db_url = env["MEIVA_DB_URL"]
    assert db_url is not None and "foreign" not in db_url
    # The cluster's secret is minted for it, never inherited from the caller's environment.
    assert env["MEIVA_CLUSTER_SECRET"] and env["MEIVA_CLUSTER_SECRET"] != "foreign-secret"  # noqa: S105


def test_interrupted_init_keeps_admitted_tool_path(
    inputs: identity.IdentityInput, site: HomeSite
) -> None:
    activated = inputs.checkout.parent / "other-venv"
    tools = str(inputs.checkout.parent / "tools")
    bin_name = "bin"
    caller = caller_host(
        path=os.pathsep.join(
            (str(activated / bin_name), str(inputs.checkout / ".venv" / bin_name), tools)
        ),
        virtual_env=str(activated),
    )
    break_env_publication(inputs.home)
    with pytest.raises(OSError):
        prepare_init_identity(_single_box(), site, caller)
    pending = identity.read_intent(inputs.home)
    assert pending is not None and pending["phase"] == "claiming"
    assert pending["env"]["MEIVA_SERVICE_PATH"] == tools
    repair_env_publication(inputs.home)
    different = caller_host(path=str(inputs.checkout.parent / "different-tools"))
    prepare_init_identity(_args(), site, different)  # the resume takes no flags
    assert dotenv_values(inputs.home / ".env")["MEIVA_SERVICE_PATH"] == tools


def test_existing_home_cannot_recapture_caller_tool_path(
    inputs: identity.IdentityInput, site: HomeSite
) -> None:
    identity.prepare_identity(inputs)
    unadmitted = caller_host(service_path=str(inputs.checkout.parent / "unadmitted"))
    with pytest.raises(ValueError, match="explicit MEIVA_SERVICE_PATH"):
        init_intent.service_path(identity.stored_values(inputs.home), site, unadmitted)


def test_lossy_tool_path_is_rejected_before_identity_publication(
    inputs: identity.IdentityInput, site: HomeSite
) -> None:
    lossy = caller_host(path=str(inputs.checkout.parent / "tools #1"))
    with pytest.raises(ValueError, match="round-trip literally"):
        prepare_init_identity(_single_box(), site, lossy)
    assert identity.read_intent(inputs.home) is None


@pytest.mark.parametrize(
    "key",
    ["MEIVA_HOME", "MEIVA_GATEWAY_PORT", "MEIVA_DB_URL", "MEIVA_MACHINE_NAME", "UNKNOWN_SETTING"],
)
def test_config_file_cannot_choose_identity(inputs: identity.IdentityInput, key: str) -> None:
    config = inputs.home.parent / "input.env"
    config.write_text(f"{key}=wrong\n")
    with pytest.raises(ValueError, match="config file"):
        init_intent.config_values(_args("--config-file", str(config)), inputs.home)


_RUNNER_ENDPOINT = "postgresql://meiva@remote.invalid/db"


# meiva:note
# A gateway that serves the database endpoint only: bootstrap serves no login.
_BOOTSTRAP = ("GET", "/api/bootstrap")
_SERVED = Reply(
    body={"MEIVA_DB_URL": _RUNNER_ENDPOINT, "MEIVA_REDIS_URL": "redis://remote.invalid/0"}
)


# meiva:note
# A remote runner's first start against the gateway at `gateway_url`.
def _runner_start(monkeypatch: pytest.MonkeyPatch, gateway_url: str, *extra: str) -> Any:
    # meiva:note
    # A remote unit joins without the human secret: its capability authenticates.
    monkeypatch.delenv("MEIVA_CLUSTER_SECRET", raising=False)
    return _args(
        "--serve-agent-runner",
        "--no-serve-gateway",
        "--machine-name",
        "runner",
        "--machine-host",
        "runner.invalid",
        "--gateway-url",
        gateway_url,
        *extra,
    )


def test_runner_without_a_capability_refuses_before_persisting(
    inputs: identity.IdentityInput,
    site: HomeSite,
    caller: CallerHost,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seed_write_generation: Any,
) -> None:
    """Bootstrap serves no login, so a runner's first start needs its unit
    capability; without one it refuses before any identity is written."""
    from base.host.db_authority import unit

    with serving({_BOOTSTRAP: _SERVED}) as served:
        args = _runner_start(monkeypatch, served.url)
        with pytest.raises(ValueError, match="holds no database capability"):
            prepare_init_identity(args, site, caller)
        assert not (inputs.home / ".env").exists()
        assert served.requests == []
        # An installed capability (a bundle the operator carried earlier) admits it.
        gateway = tmp_path / "gateway"
        gateway.mkdir(mode=0o700)
        seed_write_generation(gateway)
        home = inputs.home.resolve()
        issued = unit.issue_bundle(
            gateway.resolve(),
            unit=unit.UnitIdentity(machine="runner", home=str(home)),
            endpoint=_RUNNER_ENDPOINT,
            cluster_secret="gateway-human-secret-" + "g" * 32,
            ttl_s=60,
        )
        unit.install_bundle(
            home,
            unit.open_bundle(issued.envelope, issued.transport_key),
            machine="runner",
            served_endpoint=_RUNNER_ENDPOINT,
            probe=lambda _dsn: None,
        )
        prepare_init_identity(args, site, caller)
    env = dotenv_values(inputs.home / ".env")
    assert env["MEIVA_MACHINE_HOST"] == "runner.invalid"
    assert "MEIVA_DB_URL" not in env and "MEIVA_REDIS_URL" not in env
    # The fetch authenticated with the capability's API token; no bearer persisted.
    installed = unit.require_unit_capability(home)
    assert [seen.headers["authorization"] for seen in served.requests] == [
        f"Bearer {installed.api.token}"
    ]
    assert "MEIVA_CLUSTER_SECRET" not in env
    assert cluster.get_record(home) is None


def test_capability_bundle_needs_its_transport_key(
    inputs: identity.IdentityInput,
    site: HomeSite,
    caller: CallerHost,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "unit.bundle"
    bundle.write_text("{}")
    args = _runner_start(monkeypatch, closed_port_url(), "--db-capability", str(bundle))
    with pytest.raises(ValueError, match="MEIVA_DB_CAPABILITY_KEY"):
        prepare_init_identity(args, site, caller)
    assert bundle.exists()
    assert not (inputs.home / ".env").exists()


def test_gateway_init_refuses_a_capability_bundle(
    inputs: identity.IdentityInput, site: HomeSite, caller: CallerHost, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="agent-runner units"):
        prepare_init_identity(
            _single_box("--db-capability", str(tmp_path / "unit.bundle")), site, caller
        )
    assert identity.read_intent(inputs.home) is None


def test_ready_phase_never_regresses_or_rewrites(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    identity.mark_phase(inputs.home, "ready")
    path = inputs.home / identity.INTENT_NAME
    before = path.read_bytes(), path.stat().st_mtime_ns
    identity.mark_phase(inputs.home, "provisioned")
    identity.mark_phase(inputs.home, "ready")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_birth_records_the_ports_a_new_home_gets(
    inputs: identity.IdentityInput, session_ports: dict[str, int]
) -> None:
    """A test home records this session's own ports, never the fixed table's: the
    table is what the operator's cluster on the same box binds."""
    identity.prepare_identity(inputs)
    rec = cluster.get_record(inputs.home)
    assert rec is not None
    assert dict(rec.ports) == session_ports
    assert not set(session_ports.values()) & set(cluster.FIXED_PORTS.values())


def test_birth_refuses_ports_that_are_already_bound(
    inputs: identity.IdentityInput, session_ports: dict[str, int]
) -> None:
    held = replace(inputs, occupied_ports=frozenset({session_ports["gateway"]}))
    with pytest.raises(RuntimeError, match="already occupied"):
        identity.prepare_identity(held)
    assert not (inputs.home / identity.INTENT_NAME).exists()


def test_the_fixed_table_is_a_valid_reservation(inputs: identity.IdentityInput) -> None:
    """A production home records exactly the fixed table; its intent reads back
    unchanged."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["record"]["ports"] = dict(cluster.FIXED_PORTS)
    path.write_text(json.dumps(data))
    read = identity.read_intent(inputs.home)
    assert read is not None and read["record"]["ports"] == cluster.FIXED_PORTS


@pytest.mark.parametrize(
    ("slot", "present", "message"),
    [
        ("retired_slot", False, r"unexpected \['retired_slot'\]"),
        ("memory_search", True, r"missing \['memory_search'\]"),
    ],
)
def test_reservation_must_match_the_fixed_table_exactly(
    inputs: identity.IdentityInput, slot: str, present: bool, message: str
) -> None:
    """A record carrying a slot the table lacks (or lacking one it has) is refused
    at once and names the difference, so a hand step left undone fails the start
    loudly instead of running on a stale layout."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    if present:
        del data["record"]["ports"][slot]
    else:
        data["record"]["ports"][slot] = 8102
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match=rf"invalid start port reservation.*{message}"):
        identity.read_intent(inputs.home)


def test_corrupt_foreign_reservation_never_publishes(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["phase"] = "claiming"
    data["record"]["gateway_home"] = str(inputs.home.parent / "foreign")
    path.write_text(json.dumps(data))
    before = (inputs.home / ".env").read_bytes()
    with pytest.raises(RuntimeError, match="another home"):
        identity.resume_claim(inputs.home)
    assert (inputs.home / ".env").read_bytes() == before


def test_config_file_is_taken_once_and_a_second_init_is_refused(
    inputs: identity.IdentityInput, site: HomeSite, caller: CallerHost
) -> None:
    config = inputs.home.parent / "config.env"
    config.write_text("MEIVA_LLM_OVERRIDE=fixture:model\n")
    args = _single_box("--config-file", str(config))
    prepare_init_identity(args, site, caller)
    first = (inputs.home / ".env").read_bytes()
    assert b"MEIVA_LLM_OVERRIDE" in first
    config.write_text("MEIVA_LLM_OVERRIDE=other:model\n")
    with pytest.raises(RuntimeError, match="already initialized"):
        prepare_init_identity(args, site, caller)
    assert (inputs.home / ".env").read_bytes() == first
    # A home that already has an `.env` without an intent takes no config file either.
    with pytest.raises(ValueError, match=r"no `\.env` yet"):
        init_intent.config_values(args, inputs.home)


# meiva:note
# A single box whose data plane is a provider's. Hosts are IP literals, so no lookup is made.
def _remote_config(
    inputs: identity.IdentityInput,
    *,
    host: str = "192.0.2.55",
    machine_host: str | None = None,
    query: str = "",
) -> Any:
    config = inputs.home.parent / "remote.env"
    config.write_text(
        f"MEIVA_DB_URL=postgresql://owner:provider@{host}:6543/app{query}\nMEIVA_REDIS_URL=rediss://acl:provider@192.0.2.56:6381/0\nMEIVA_RUNNER_DB_PASSWORD=external-runner\n"
    )
    own_address = () if machine_host is None else ("--machine-host", machine_host)
    return _single_box("--config-file", str(config), *own_address)


def test_remote_init_preserves_provider_urls_and_runner_credential(
    inputs: identity.IdentityInput, site: HomeSite, caller: CallerHost
) -> None:
    args = _remote_config(inputs)
    prepare_init_identity(args, site, caller)
    env = dotenv_values(inputs.home / ".env")
    assert env["MEIVA_DB_URL"] == "postgresql://owner:provider@192.0.2.55:6543/app"
    assert env["MEIVA_REDIS_URL"] == "rediss://acl:provider@192.0.2.56:6381/0"
    assert env["MEIVA_RUNNER_DB_PASSWORD"] == "external-runner"  # noqa: S105 — fixture credential
    assert "MEIVA_DB_ADMIN_PASSWORD" not in env
    assert "MEIVA_REDIS_ADMIN_PASSWORD" not in env
    before = (inputs.home / ".env").read_bytes()
    with pytest.raises(RuntimeError, match="already initialized"):
        prepare_init_identity(args, site, caller)
    assert (inputs.home / ".env").read_bytes() == before


@pytest.mark.parametrize(
    ("host", "machine_host"),
    [
        ("127.0.0.1", None),
        ("localhost", None),
        (socket.gethostname(), None),
        ("192.0.2.1", "192.0.2.1"),
    ],
    ids=["loopback", "localhost", "this-hostname", "the-unit's-own-address"],
)
def test_remote_input_refuses_local_or_mixed_ownership(
    inputs: identity.IdentityInput,
    site: HomeSite,
    caller: CallerHost,
    host: str,
    machine_host: str | None,
) -> None:
    args = _remote_config(inputs, host=host, machine_host=machine_host)
    with pytest.raises(ValueError, match="foreign"):
        prepare_init_identity(args, site, caller)
    assert identity.read_intent(inputs.home) is None


@pytest.mark.parametrize("query", ["?hostaddr=127.0.0.1", "?host=", "?port=", "?dbname=other"])
def test_remote_input_cannot_redirect_libpq_identity(
    inputs: identity.IdentityInput, site: HomeSite, caller: CallerHost, query: str
) -> None:
    args = _remote_config(inputs, query=query)
    with pytest.raises(ValueError, match="redirect"):
        prepare_init_identity(args, site, caller)
    assert identity.read_intent(inputs.home) is None


# meiva:note
# Admission holds the lifecycle lock while the lifecycle runs, and discards the
# inherited identity of another home. Entering a home rewrites the process's environment, so
# a child process does it, started in another home with that home's login in its environment.
def test_an_admitted_home_holds_the_lock_and_enters_the_home(
    inputs: identity.IdentityInput, site: HomeSite, caller: CallerHost, tmp_path: Path
) -> None:
    prepare_init_identity(_single_box(), site, caller)
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(
        MEIVA_HOME=str(tmp_path / "other-home"),
        MEIVA_DB_URL="postgresql://foreign@foreign.invalid/production",
    )

    # meiva:note
    # The child is this repository's own fixed probe script, run with the interpreter in
    # isolated mode.
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(_ADMITTED_HOME), str(inputs.home)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert child.returncode == 0, child.stderr


def test_failed_boot_publication_clears_serving_and_refuses_success(
    inputs: identity.IdentityInput,
    site: HomeSite,
    caller: CallerHost,
    monkeypatch: pytest.MonkeyPatch,
    command_runtime: CommandRuntime,
) -> None:
    prepare_init_identity(_single_box(), site, caller)

    def fail(**_kw: object) -> None:
        raise RuntimeError("manager refused native custody")

    # meiva:note
    # The boot unit's manager is the one fault a real resource cannot produce here: the
    # hand-off asks the process's own systemd cgroup.
    monkeypatch.setattr(root_driver, "complete_boot_start", fail)
    with start_intent.admitted_home(inputs.home):
        start_serving.begin_start(inputs.home)
        assert start_serving.state_path(inputs.home).exists()
        with pytest.raises(RuntimeError, match="refused native custody"):
            start_intent.finish_start(0, helper=None, host=command_runtime.host, home=inputs.home)
        assert not start_serving.state_path(inputs.home).exists(), (
            "the start no longer reads as serving"
        )


def _initialized_gateway_home(inputs: identity.IdentityInput) -> Path:
    """A born gateway home that has finished its claim, as `meiva start` finds it."""
    identity.prepare_identity(replace(inputs, roles=frozenset({"gateway"})))
    env = dotenv_values(inputs.home / ".env")
    with (inputs.home / ".env").open("a") as handle:
        handle.write("MEIVA_SERVICE_PATH=/usr/bin:/bin\n")
    assert env["MEIVA_CLUSTER_SECRET"]
    return inputs.home


def test_start_admits_a_gateway_home_that_holds_its_secret(inputs: identity.IdentityInput) -> None:
    home = _initialized_gateway_home(inputs)
    assert identity.require_initialized(home).roles == frozenset({"gateway"})


def test_start_refuses_a_gateway_home_without_a_cluster_secret(
    inputs: identity.IdentityInput,
) -> None:
    """A hand-edited home, or one born before every cluster had a secret, is refused
    with the instruction to set one, before any native effect."""
    from base.host.env.dotenv_file import upsert_env
    from base.host.net.auth import ClusterSecretMissingError

    home = _initialized_gateway_home(inputs)
    upsert_env(home / ".env", {"MEIVA_CLUSTER_SECRET": ""})
    with pytest.raises(ClusterSecretMissingError, match="MEIVA_CLUSTER_SECRET"):
        identity.require_initialized(home)
