# meiva:note
# `meiva start` admits only a home `meiva init` initialized, and takes no identity input.

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.native_process.os_platform import file_lock
from cli import start_identity
from cli.startup.tests.birth_facts import birth_facts
from cli.tests.guards.start_refusal_child import SINK_SENTINEL

_GATEWAY = {"gateway", "agent-runner"}


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
            home,
            checkout,
            frozenset(roles),
            values,
            occupied_ports=frozenset(),
            birth=birth_facts(),
        )
    )
    return home


# meiva:note
# Entering a home rewrites the child's environment, leaving this pytest worker untouched.
def _admit_in_child(home: Path, tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    user_home.mkdir()
    child = run_bounded(
        [sys.executable, "-I", "-B", "-m", "cli.tests.bootstrap.child_admitted_home", str(home)],
        cwd=user_home,
        env={
            "HOME": str(user_home),
            "MEIVA_HOME": str(tmp_path / "foreign-home"),
            "MEIVA_CONFIG_FETCH": "skip",
            "PATH": "/usr/bin:/bin",
            "MEIVA_DB_URL": "postgresql://foreign@foreign.invalid/foreign",
            "MEIVA_DB_GENERATION": "42",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr


# meiva:note
# Refusal must precede real logging; unusable configuration also fences off a valid start.
def _run_start_in_child(home: Path, checkout: Path, tmp_path: Path) -> tuple[int, str]:
    user_home = tmp_path / "user"
    user_home.mkdir()
    child = run_bounded(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "cli.tests.guards.start_refusal_child",
            str(home),
            str(checkout),
        ],
        cwd=user_home,
        env={
            "HOME": str(user_home),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_PROCESS_PROFILE": "not-a-profile",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert SINK_SENTINEL not in child.stderr, "a refused start opened a real stderr sink"
    assert not (home / "logs").exists()
    code: int = json.loads(child.stdout)["code"]
    return code, child.stderr


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
) -> None:
    home = tmp_path / "never-initialized"

    rc, error = _run_start_in_child(home, tmp_path / "checkout", tmp_path)

    assert rc == 1
    assert "meiva start: " in error
    assert "run `meiva init` first" in error
    assert not home.exists()


# meiva:note
# The runtime is admitted under the home's lock and before any sink opens: a checkout that did
# not load this code is refused by name, and the lock is free for the next lifecycle operation.
def test_a_start_from_a_checkout_that_did_not_load_the_code_is_refused_before_any_sink(
    tmp_path: Path,
    tools: str,
) -> None:
    home = _home(tmp_path, tools, _GATEWAY)
    before = _bytes(home)
    entries = {path.name for path in home.iterdir()} | {"start-intent.lock"}

    rc, error = _run_start_in_child(home, tmp_path / "checkout", tmp_path)

    assert rc == 1
    assert "differs from its loaded canonical checkout" in error
    assert _bytes(home) == before
    assert {path.name for path in home.iterdir()} == entries
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

    _admit_in_child(home, tmp_path)

    assert _bytes(home) == before


# meiva:note
# The join (bundle, bootstrap fetch) belongs to init and install-unit: a started
# runner fetches its configuration through Settings and probes its gateway itself.
# Nothing listens at the gateway named here, so a start that dialed it would fail.
def test_start_never_joins_a_gateway(tmp_path: Path, tools: str) -> None:
    home = _home(tmp_path, tools, {"agent-runner"}, MEIVA_GATEWAY_URL="http://127.0.0.1:1")

    _admit_in_child(home, tmp_path)


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
