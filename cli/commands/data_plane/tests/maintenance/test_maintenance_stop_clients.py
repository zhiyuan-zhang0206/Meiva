# meiva:note
# The data-plane stop names the clients still on the pooler before it stops it, and only
# reports: nothing here changes the stop.
#
# The lines themselves are asserted as values in `maintenance/test_report_lines.py`. Here the
# console is read from a real pooler with the admin credential of a real home: clients that are
# really connected, a console with none, and a console that cannot be read.

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from base.host.db_authority import delivery
from cli.commands.data_plane import maintenance_stop
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.tests.native_processes import native_pooler as native_pooler
from cli.commands.data_plane.tests.native_processes import running_pooler

# meiva:note
# A console credential that is not the home's, to prove a console refusing the home's credential
# is reported as unreadable. It is a fixture value, not a secret.
_OTHER_CONSOLE_PASSWORD = "a-different-console-password"  # noqa: S105


pytestmark = pytest.mark.usefixtures("postgres_access")


def _encrypt(name: str, _password: str) -> str:
    return f"SCRAM-SHA-256$4096:c2VlZA==${name}"


def _admin_password(home: Path) -> str:
    return delivery.ensure_pooler_admin(home, encrypt=_encrypt).password


def test_remaining_clients_are_named_in_stderr_and_returned_for_the_report(
    native_pooler: tuple[OwnedPooler, str, str],
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    custodian, pooled, _direct = native_pooler
    _admin_password(unit_home)
    with (
        psycopg.connect(pooled, prepare_threshold=None, application_name="probe-a") as first,
        psycopg.connect(pooled, prepare_threshold=None, application_name="probe-b") as second,
    ):
        first.execute("SELECT 1")
        second.execute("SELECT 1")

        line = maintenance_stop.report_pooler_clients(custodian.identity, home=unit_home)

    err = capsys.readouterr().err
    assert "2 client(s) still connected when the pooler stops" in err
    assert "app=probe-a" in err and "app=probe-b" in err
    assert "user=meiva state=" in err
    assert line == err.strip().removeprefix("! ")


def test_a_console_with_no_clients_says_nothing(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_pooler(unit_home, console_password=_admin_password(unit_home)) as running:
        line = maintenance_stop.report_pooler_clients(running.identity, home=unit_home)

    assert capsys.readouterr().err == "" and line is None


# meiva:note
# A console that cannot be read is reported as that, never as an empty list: this home holds
# no admin credential at all.
def test_a_home_without_an_admin_credential_is_reported_as_unreadable_not_as_empty(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_pooler(unit_home) as running:
        line = maintenance_stop.report_pooler_clients(running.identity, home=unit_home)

    assert "could not be listed before the pooler stop" in capsys.readouterr().err
    assert line is not None and "no pooler admin credential" in line


def test_a_console_that_refuses_the_credential_is_reported_as_unreadable(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _admin_password(unit_home)
    with running_pooler(unit_home, console_password=_OTHER_CONSOLE_PASSWORD) as running:
        line = maintenance_stop.report_pooler_clients(running.identity, home=unit_home)

    assert "could not be listed before the pooler stop" in capsys.readouterr().err
    assert line is not None and "OperationalError" in line
