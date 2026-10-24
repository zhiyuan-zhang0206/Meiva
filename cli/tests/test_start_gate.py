# meiva:note
# `meiva start` admits only a home `meiva init` initialized, and takes no identity input.

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from base.native_process.os_platform import file_lock
from cli import start_identity, start_intent
from cli.database import OperatorDatabaseFactory, operator_event_pipeline
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from cli.start_runtime import HomeSite

_GATEWAY = {"gateway", "agent-runner"}


@pytest.fixture(autouse=True)
def _isolate_environment() -> Iterator[None]:
    """A real start prunes derived keys from the process; that ends with the test."""
    with patch.dict(os.environ):
        yield


@pytest.fixture
def tools(tmp_path: Path) -> str:
    return str(tmp_path / "tools")


# meiva:note
# A home `meiva init` would have left: intent and `.env` published.
def _home(tmp_path: Path, tools: str, roles: set[str], **extra: str) -> Path:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    values = {"MEIVA_MACHINE_NAME": "gate", "MEIVA_SERVICE_PATH": tools, **extra}
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home, checkout, frozenset(roles), values, occupied_ports=frozenset()
        )
    )
    return home


def _site(home: Path, tmp_path: Path) -> HomeSite:
    return HomeSite(home, tmp_path / "checkout")


# meiva:note
# Why `meiva start` refuses `home`: the admission is the gate every start passes first.
def _refusal(home: Path) -> str:
    with pytest.raises((RuntimeError, ValueError)) as refused:
        start_identity.require_initialized(home)
    return str(refused.value)


def _bytes(home: Path) -> list[tuple[bytes, int]]:
    return [
        (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (home / ".env", home / start_identity.INTENT_NAME)
    ]


def test_a_missing_home_is_refused_and_nothing_is_made(tmp_path: Path) -> None:
    home = tmp_path / "never-initialized"

    assert "not initialized: run `meiva init` first" in _refusal(home)
    assert not home.exists()


def test_start_reports_a_refusal_on_stderr_opens_no_sink_and_makes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cli_log_sinks: list[str],
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> None:
    home = tmp_path / "never-initialized"
    producer = operator_event_pipeline(
        operator_database, machine=command_runtime.machine, home=home
    )
    args = build_parser().parse_args(["start", "--all-services"])
    args.runtime = replace(command_runtime, home=home)

    rc = start_intent.run_start(
        args,
        _site(home, tmp_path),
        retained_children=[],
        database_factory=operator_database,
        producer=producer,
    )

    assert rc == 1
    assert "meiva start: " in capsys.readouterr().err
    assert cli_log_sinks == [], "sinks build Settings: a refused home never gets them"
    assert not home.exists()


# meiva:note
# The runtime is admitted under the home's lock and before any sink opens: a checkout that did
# not load this code is refused by name, and the lock is free for the next lifecycle operation.
def test_a_start_from_a_checkout_that_did_not_load_the_code_is_refused_before_any_sink(
    tmp_path: Path,
    tools: str,
    capsys: pytest.CaptureFixture[str],
    cli_log_sinks: list[str],
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> None:
    home = _home(tmp_path, tools, _GATEWAY)
    producer = operator_event_pipeline(
        operator_database, machine=command_runtime.machine, home=home
    )
    args = build_parser().parse_args(["start", "--all-services"])
    args.runtime = replace(command_runtime, home=home)

    rc = start_intent.run_start(
        args,
        _site(home, tmp_path),
        retained_children=[],
        database_factory=operator_database,
        producer=producer,
    )

    assert rc == 1
    assert "differs from its loaded canonical checkout" in capsys.readouterr().err
    assert cli_log_sinks == []
    with file_lock(home / "start-intent.lock", timeout_s=0.05):
        pass


def test_resources_without_an_intent_are_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "pg").mkdir(parents=True)

    assert "no initialization authority" in _refusal(home)


def test_a_claim_in_progress_sends_the_operator_back_to_init(tmp_path: Path, tools: str) -> None:
    home = _home(tmp_path, tools, _GATEWAY)
    path = home / start_identity.INTENT_NAME
    path.write_text(path.read_text().replace('"phase": "configured"', '"phase": "claiming"'))

    refusal = _refusal(home)

    assert "partly initialized" in refusal and "Re-run `meiva init`" in refusal


def test_a_detached_home_is_refused(tmp_path: Path, tools: str) -> None:
    home = _home(tmp_path, tools, _GATEWAY)
    (home / "destroy-intent.json").write_text('{"version":1,"state":"detached"}\n')

    assert "destroyed or detached" in _refusal(home)


@pytest.mark.parametrize("phase", ["configured", "provisioned", "ready"])
def test_an_initialized_home_is_admitted_and_left_as_it_was(
    tmp_path: Path, tools: str, phase: str
) -> None:
    home = _home(tmp_path, tools, _GATEWAY)
    start_identity.mark_phase(home, phase)
    before = _bytes(home)

    with start_intent.admitted_home(home):
        pass

    assert _bytes(home) == before


# meiva:note
# The join (bundle, bootstrap fetch) belongs to init and install-unit: a started
# runner fetches its configuration through Settings and probes its gateway itself.
# Nothing listens at the gateway named here, so a start that dialed it would fail.
def test_start_never_joins_a_gateway(tmp_path: Path, tools: str) -> None:
    home = _home(tmp_path, tools, {"agent-runner"}, MEIVA_GATEWAY_URL="http://127.0.0.1:1")

    with start_intent.admitted_home(home):
        pass


def _rewrite_env(home: Path, edit: Callable[[list[str]], list[str]]) -> None:
    env = home / ".env"
    env.write_text("\n".join(edit(env.read_text().splitlines())) + "\n")


@pytest.mark.parametrize(
    ("roles", "drop", "message"),
    [
        (_GATEWAY, "MEIVA_DB_URL=", "incomplete; explicit reattachment is required"),
        (_GATEWAY, "MEIVA_REDIS_URL=", "incomplete; explicit reattachment is required"),
        ({"agent-runner"}, "MEIVA_GATEWAY_URL=", "no gateway identity"),
        (_GATEWAY, "MEIVA_SERVICE_PATH=", "explicit MEIVA_SERVICE_PATH declaration"),
    ],
)
def test_an_incomplete_home_is_refused_before_any_effect(
    tmp_path: Path, tools: str, roles: set[str], drop: str, message: str
) -> None:
    extra = {} if "gateway" in roles else {"MEIVA_GATEWAY_URL": "http://10.0.0.7:8000"}
    home = _home(tmp_path, tools, roles, **extra)
    _rewrite_env(home, lambda lines: [ln for ln in lines if not ln.startswith(drop)])

    assert message in _refusal(home)


def test_a_remote_unit_that_records_the_human_secret_is_refused(tmp_path: Path, tools: str) -> None:
    home = _home(tmp_path, tools, {"agent-runner"}, MEIVA_GATEWAY_URL="http://10.0.0.7:8000")
    _rewrite_env(home, lambda lines: [*lines, "MEIVA_CLUSTER_SECRET=leaked"])

    assert "records the human cluster secret" in _refusal(home)


@pytest.mark.parametrize(
    "env",
    [
        pytest.param("MEIVA_GATEWAY_URL=http://10.0.0.7:8000\n", id="runner"),
        pytest.param("MEIVA_DB_URL=postgresql://db.invalid/meiva\n", id="gateway"),
    ],
)
# meiva:note
# A home with no `start-intent.json` has no admission, whatever its `.env` and capability flags
# declare: `meiva start` takes an identity only from `meiva init`.
def test_a_home_with_an_identity_but_no_intent_is_refused(
    tmp_path: Path, tools: str, env: str
) -> None:
    home = tmp_path / "predates-the-intent"
    home.mkdir(mode=0o700)
    (home / ".env").write_text(
        f"{env}MEIVA_SERVICE_PATH={tools}\nMEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
    )

    assert "no recorded identity" in _refusal(home)
    assert not (home / start_identity.INTENT_NAME).exists(), "a start never writes an identity"
