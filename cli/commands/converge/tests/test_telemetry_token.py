# meiva:note
# The telemetry-token converge step: a gateway home keeps one random OTLP bearer, drawn per call
# and kept once published.

from __future__ import annotations

import random
import stat
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest

from base.host.db_authority.api import read_telemetry_token, telemetry_token_path
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.converge.telemetry_token import ensure_telemetry_token_step
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _never_dialed() -> NoReturn:
    raise AssertionError("this step must not open the database")


def _never_produced() -> EventPipeline:
    raise AssertionError("this step must not emit events")


@pytest.fixture
def ctx(tmp_path: Path, command_runtime: CommandRuntime) -> ConvergeCtx:
    return ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=tmp_path / "repo",
        meiva_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=_never_dialed,
        producer=_never_produced,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=tmp_path / "user", login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
        services=frozenset(),
        terminal=command_runtime.terminal,
    )


def test_a_gateway_home_without_a_token_is_given_a_random_one_owner_only(ctx: ConvergeCtx) -> None:
    ensure_telemetry_token_step(ctx)

    token = read_telemetry_token(ctx.meiva_home.resolve())
    assert token is not None and len(token) >= 32
    assert stat.S_IMODE(telemetry_token_path(ctx.meiva_home.resolve()).stat().st_mode) == 0o600


def test_a_later_converge_keeps_the_token_it_finds(ctx: ConvergeCtx) -> None:
    ensure_telemetry_token_step(ctx)
    first = read_telemetry_token(ctx.meiva_home.resolve())

    ensure_telemetry_token_step(ctx)

    assert read_telemetry_token(ctx.meiva_home.resolve()) == first


def test_two_homes_are_never_given_the_same_token(ctx: ConvergeCtx) -> None:
    other = replace(ctx, meiva_home=ctx.meiva_home.parent / "other-home")

    ensure_telemetry_token_step(ctx)
    ensure_telemetry_token_step(other)

    assert read_telemetry_token(ctx.meiva_home.resolve()) != read_telemetry_token(
        other.meiva_home.resolve()
    )
