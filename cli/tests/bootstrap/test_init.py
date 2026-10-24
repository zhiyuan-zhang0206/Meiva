"""`meiva init` records a home's identity once, starts nothing, and refuses a second run."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest
from dotenv import dotenv_values

from base import cluster
from cli import init_intent, start_identity
from cli.parsers import build_parser
from cli.start_runtime import HomeSite
from cli.tests.bootstrap.init_identity import (
    break_env_publication,
    caller_host,
    repair_env_publication,
    site_in,
)


@pytest.fixture
def site(tmp_path: Path) -> HomeSite:
    return site_in(tmp_path)


@pytest.fixture
def home(site: HomeSite) -> Path:
    return site.home


class _Init(Protocol):
    def __call__(self, *flags: str, occupied_ports: frozenset[int] = ...) -> int: ...


@pytest.fixture
def init(site: HomeSite, tmp_path: Path) -> _Init:
    """`meiva init` as the entry hands it its site: a caller declaring its tool directories."""
    caller = caller_host(service_path=str(tmp_path / "tools"))

    def run(*flags: str, occupied_ports: frozenset[int] = frozenset()) -> int:
        args = build_parser().parse_args(["init", *flags])
        return init_intent.run_init(args, site, replace(caller, occupied_ports=occupied_ports))

    return run


_SINGLE_BOX = ("--serve-gateway", "--serve-agent-runner", "--machine-name", "probe")


def _state(home: Path) -> list[tuple[bytes, int]]:
    paths = [home / ".env", home / start_identity.INTENT_NAME]
    return [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]


def test_init_publishes_the_identity_starts_nothing_and_names_the_next_step(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    assert init(*_SINGLE_BOX) == 0

    intent = start_identity.read_intent(home)
    assert intent is not None and intent["phase"] == "configured"
    assert intent["roles"] == ["agent-runner", "gateway"]
    env = dotenv_values(home / ".env")
    assert env["MEIVA_MACHINE_NAME"] == "probe" and env["MEIVA_DB_URL"]
    # No native resource exists: the first `meiva start` provisions the data plane.
    assert not any((home / name).exists() for name in ("pg", "redis", "pgbouncer", "run"))
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("next: run `") and last.endswith(" start`")


@pytest.mark.parametrize("phase", ["configured", "provisioned", "ready"])
def test_init_refuses_an_initialized_home_and_changes_nothing(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    assert init(*_SINGLE_BOX) == 0
    start_identity.mark_phase(home, phase)
    before = _state(home)
    capsys.readouterr()

    assert init(*_SINGLE_BOX) == 1

    err = capsys.readouterr().err
    assert f"already initialized (phase {phase})" in err
    assert "`meiva start`" in err and "`meiva cluster destroy`" in err
    assert _state(home) == before


def test_an_interrupted_claim_resumes_from_its_recorded_payload_without_flags(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    break_env_publication(home)
    assert init(*_SINGLE_BOX) == 1
    pending = start_identity.read_intent(home)
    assert pending is not None and pending["phase"] == "claiming"
    assert not (home / ".env").exists()

    repair_env_publication(home)
    assert init() == 0
    assert cluster.get_record(home) == cluster.ClusterRecord(**pending["record"])
    assert dict(dotenv_values(home / ".env")) == pending["env"]
    done = start_identity.read_intent(home)
    assert done is not None and done["phase"] == "configured" and done["env"] == {}
    assert "initialized" in capsys.readouterr().out


def test_an_interrupted_claim_takes_no_flags(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    break_env_publication(home)
    assert init(*_SINGLE_BOX) == 1
    capsys.readouterr()

    assert init("--machine-name", "other", "--serve-gateway") == 1

    err = capsys.readouterr().err
    assert "without flags" in err and "--machine-name" in err and "--serve-gateway" in err
    pending = start_identity.read_intent(home)
    assert pending is not None and pending["phase"] == "claiming"
    assert not (home / ".env").exists()


def test_init_refuses_a_detached_home(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    home.mkdir(mode=0o700)
    (home / "destroy-intent.json").write_text('{"version":1,"state":"detached"}\n')

    assert init(*_SINGLE_BOX) == 1

    assert "destroyed or detached" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()


def test_init_refuses_configuration_without_an_intent(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    """A home that has an identity in its `.env` but no intent has no initialization
    authority: init does not adopt it, and `meiva start` refuses it the same way."""
    home.mkdir(mode=0o700)
    (home / ".env").write_text("MEIVA_GATEWAY_URL=http://10.0.0.7:8000\n")

    assert init("--serve-agent-runner", "--no-serve-gateway", "--machine-name", "r") == 1

    assert "no recorded identity" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()


def test_init_without_inputs_names_this_checkouts_cli(
    home: Path, init: _Init, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The host's `meiva` is linked only by a first start, so the examples name the
    checkout's own `.venv/bin/meiva`."""
    assert init() == 1

    err = capsys.readouterr().err
    assert "no capability declared" in err
    assert f"{tmp_path / 'checkout'}/.venv/bin/meiva init --machine-name <name>" in err
    assert not home.exists() or not (home / start_identity.INTENT_NAME).exists()


def test_init_requires_a_machine_name(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    assert init("--serve-gateway", "--serve-agent-runner") == 1

    assert "init requires --machine-name" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()


def test_init_refuses_a_gateway_whose_ports_something_already_holds(
    home: Path, init: _Init, capsys: pytest.CaptureFixture[str]
) -> None:
    held = cluster.new_home_ports()["gateway"]

    assert init(*_SINGLE_BOX, occupied_ports=frozenset({held})) == 1

    assert "already occupied" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()
    assert not (home / ".env").exists()


_INIT_ONLY_FLAGS = [
    ["--machine-name", "x"],
    ["--serve-gateway"],
    ["--no-serve-gateway"],
    ["--serve-agent-runner"],
    ["--no-serve-agent-runner"],
    ["--serve-observability-station"],
    ["--machine-description", "x"],
    ["--memory-remote", "x"],
    ["--gateway-url", "http://x"],
    ["--config-file", "x"],
    ["--machine-host", "x"],
    ["--ssl-cert-file", "x"],
    ["--db-capability", "x"],
]
_START_ONLY_FLAGS = [
    ["--only-service", "gateway"],
    ["--disable-service", "frontend"],
    ["--all-services"],
]


@pytest.mark.parametrize("flags", _INIT_ONLY_FLAGS, ids=lambda f: f[0])
def test_start_takes_no_identity_flag(flags: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["start", *flags])
    assert error.value.code == 2
    build_parser().parse_args(["init", *flags])  # init owns it


@pytest.mark.parametrize("flags", _START_ONLY_FLAGS, ids=lambda f: f[0])
def test_init_takes_no_service_selection(flags: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["init", *flags])
    assert error.value.code == 2
    build_parser().parse_args(["start", *flags])  # start owns it
