from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from loguru import logger

from base.paths import meiva_home
from services.backup.walg import probe, startup
from services.backup.walg.archive import expected_for
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.tests.support import Sandbox, archiving_postgres, make_sandbox

# meiva:note
# loguru's sink is process-wide, so these fixtures keep only this module's records:
# "silent" has to mean "startup logged nothing", not "no other thread logged".
_MODULE = "services.backup.walg.startup"


def _captured(level: str) -> Generator[list[str]]:
    captured: list[str] = []
    handler = logger.add(
        lambda message: (
            captured.append(str(message)) if message.record["name"] == _MODULE else None
        ),
        level=level,
    )
    yield captured
    logger.remove(handler)


@pytest.fixture
def errors() -> Generator[list[str]]:
    yield from _captured("ERROR")


@pytest.fixture
def warnings() -> Generator[list[str]]:
    yield from _captured("WARNING")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _refused() -> PgTarget:
    return PgTarget("postgresql://meiva@/postgres?host=/nonexistent&port=1", Path("/x"), "postgres")


def test_a_postgres_launched_with_the_configured_settings_is_silent(
    sandbox: Sandbox, errors: list[str], warnings: list[str], postgres_access: None
) -> None:
    with archiving_postgres(sandbox=sandbox) as pg:
        message = startup.assert_configured_archive(
            machine="macmini",
            path=sandbox.path,
            admin=probe.admin_connection_to(pg.target),
            home=meiva_home(),
        )

    assert message is None
    assert errors == [] and warnings == [], "silent means the settings were read and matched"


# meiva:note
# The retained postmaster keeps the arguments it was born with: the same Postgres is judged
# against what the code would launch it with now.
def test_a_postgres_launched_with_other_settings_names_the_machine_and_both_value_sets(
    sandbox: Sandbox, errors: list[str], postgres_access: None
) -> None:
    with archiving_postgres("-c", "archive_mode=off", sandbox=sandbox) as pg:
        message = startup.assert_configured_archive(
            machine="macmini",
            path=sandbox.path,
            admin=probe.admin_connection_to(pg.target),
            home=meiva_home(),
        )

    expected = expected_for(sandbox.path, home=meiva_home())
    assert expected is not None
    assert message is not None
    assert "macmini" in message
    assert "archive_mode=off" in message and f"expected {expected.mode}" in message
    assert expected.command in message
    assert any("macmini" in line and "archive_mode=off" in line for line in errors)


def test_a_server_that_cannot_be_read_warns_and_never_raises(
    sandbox: Sandbox, errors: list[str], warnings: list[str], postgres_access: None
) -> None:
    message = startup.assert_configured_archive(
        machine="macmini",
        path=sandbox.path,
        admin=probe.admin_connection_to(_refused()),
        home=meiva_home(),
    )

    assert message is None
    assert errors == []
    assert any("macmini" in line and "unreadable" in line for line in warnings)


# meiva:note
# With the key unset the dial is never entered, so it can neither fail nor warn.
def test_walg_off_skips_the_check_entirely(sandbox: Sandbox) -> None:
    entered: list[bool] = []

    class _Dial:
        def __enter__(self) -> object:
            entered.append(True)
            raise AssertionError("no connection is opened when WAL-G is off")

        def __exit__(self, *exc: object) -> None:
            return None

    message = startup.assert_configured_archive(
        machine="macmini",
        path=None,
        admin=_Dial(),  # pyright: ignore[reportArgumentType]
        home=meiva_home(),
    )
    assert message is None
    assert entered == []
