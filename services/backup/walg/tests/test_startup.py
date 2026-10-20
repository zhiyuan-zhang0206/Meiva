from __future__ import annotations

import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from services.backup.walg import startup
from services.backup.walg.archive import ExpectedArchive, expected_archive


class _Conn:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def execute(self, statement: str) -> _Conn:
        self.statement = statement
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


def _reader() -> Callable[[], Path | None]:
    return lambda: Path(tempfile.gettempdir()) / "walg-config.json"


def _expected() -> ExpectedArchive:
    expected = expected_archive(path_reader=_reader())
    assert expected is not None, "the test environment carries a WAL-G config path"
    return expected


def _connect(row: tuple[Any, ...] | None) -> Callable[[], Any]:
    @contextmanager
    def connect() -> Generator[_Conn]:
        yield _Conn(row)

    return connect


# meiva:note
# loguru's sink is process-wide, so these fixtures keep only this module's records:
# "silent" has to mean "startup logged nothing", not "no other thread logged".
_MODULE = "services.backup.walg.startup"


@pytest.fixture
def errors() -> Generator[list[str]]:
    captured: list[str] = []
    handler = logger.add(
        lambda message: (
            captured.append(str(message)) if message.record["name"] == _MODULE else None
        ),
        level="ERROR",
    )
    yield captured
    logger.remove(handler)


@pytest.fixture
def warnings() -> Generator[list[str]]:
    captured: list[str] = []
    handler = logger.add(
        lambda message: (
            captured.append(str(message)) if message.record["name"] == _MODULE else None
        ),
        level="WARNING",
    )
    yield captured
    logger.remove(handler)


def test_matching_settings_are_silent(errors: list[str]) -> None:
    expected = _expected()
    row = (expected.mode, expected.timeout_s, expected.command)
    assert (
        startup.assert_configured_archive(
            machine="macmini", path_reader=_reader(), connect=_connect(row)
        )
        is None
    )
    assert errors == []


def test_archive_mode_off_names_the_machine_and_both_value_sets(errors: list[str]) -> None:
    expected = _expected()
    row = ("off", expected.timeout_s, expected.command)
    message = startup.assert_configured_archive(
        machine="macmini", path_reader=_reader(), connect=_connect(row)
    )
    assert message is not None
    assert "macmini" in message
    assert "archive_mode=off" in message and f"expected {expected.mode}" in message
    assert any("macmini" in line and "archive_mode=off" in line for line in errors)


def test_a_replaced_archive_command_is_reported(errors: list[str]) -> None:
    expected = _expected()
    row = (expected.mode, expected.timeout_s, "/bin/false %p")
    message = startup.assert_configured_archive(
        machine="company-air", path_reader=_reader(), connect=_connect(row)
    )
    assert message is not None
    assert "/bin/false %p" in message and "company-air" in message


def test_a_different_timeout_is_reported() -> None:
    expected = _expected()
    row = (expected.mode, expected.timeout_s + 1, expected.command)
    assert (
        startup.assert_configured_archive(
            machine="macmini", path_reader=_reader(), connect=_connect(row)
        )
        is not None
    )


def test_an_unreadable_server_warns_and_never_raises(
    errors: list[str], warnings: list[str]
) -> None:
    @contextmanager
    def broken() -> Generator[Any]:
        raise RuntimeError("permission denied for relation pg_settings")
        yield

    assert (
        startup.assert_configured_archive(machine="macmini", path_reader=_reader(), connect=broken)
        is None
    )
    assert errors == []
    assert any("macmini" in line and "unreadable" in line for line in warnings)


def test_walg_off_skips_the_check_entirely() -> None:
    def never_called() -> Any:
        raise AssertionError("no connection is opened when WAL-G is off")

    assert (
        startup.assert_configured_archive(
            machine="macmini", path_reader=lambda: None, connect=never_called
        )
        is None
    )


def test_a_server_that_returns_no_row_warns_rather_than_raising(
    warnings: list[str],
) -> None:
    assert (
        startup.assert_configured_archive(
            machine="macmini", path_reader=_reader(), connect=_connect(None)
        )
        is None
    )
    assert any("macmini" in line and "no archive settings" in line for line in warnings)
