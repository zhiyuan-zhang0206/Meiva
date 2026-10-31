from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.backup.walg import check, probe
from services.backup.walg import config as walg_config
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.tests.support import (
    SECRETS,
    Sandbox,
    archiving_postgres,
    make_sandbox,
    valid_config,
)

_REFUSED = PgTarget("postgresql://meiva@/postgres?host=/nonexistent&port=1", Path("/x"), "postgres")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_access: None) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _run_check(
    sandbox: Sandbox,
    *,
    token: str = "a" * 32,
    binary_problem: str | None = None,
    admin: AbstractContextManager[psycopg.Connection[Any]] | None = None,
) -> list[check.Step]:
    return check.run_check_on(
        path=sandbox.path,
        token=token,
        binary_problem=binary_problem,
        admin=probe.admin_connection_to(_REFUSED) if admin is None else admin,
        home=sandbox.home,
        environ=sandbox.environ,
    )


def _verdicts(steps: list[check.Step]) -> list[tuple[str, bool]]:
    return [(step.name, step.ok) for step in steps]


def test_off_is_a_single_failing_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)

    steps = _run_check(sandbox)

    assert _verdicts(steps) == [("configured", False)]
    assert "MEIVA_WALG_CONFIG_FILE is not set" in steps[0].detail


def test_a_working_setup_passes_every_step_and_leaves_nothing_in_the_store(
    sandbox: Sandbox,
) -> None:
    steps = _run_check(sandbox)

    assert _verdicts(steps) == [
        ("configured", True),
        ("binary", True),
        ("configuration", True),
        ("storage", True),
        ("postgres", True),
    ]
    assert sandbox.stored() == [], "the check deletes what it wrote"
    kinds = [call.split()[0:2] for call in sandbox.calls()]
    assert kinds == [["st", "put"], ["st", "ls"], ["st", "get"], ["st", "rm"], ["st", "ls"]]
    put = sandbox.calls()[0].split()
    assert put[3].startswith("meiva-check/") and put[3].endswith(".txt")
    assert sandbox.calls()[2].split()[2] == put[3] + ".lz4", "`st get` takes the stored .lz4 name"
    assert "not pinned yet" in next(s.detail for s in steps if s.name == "configuration")
    assert not any(secret in step.detail for step in steps for secret in SECRETS)


def test_the_check_reports_the_key_the_handed_home_pinned(sandbox: Sandbox) -> None:
    walg_config.load_walg_config(path=sandbox.path, home=sandbox.home)

    steps = _run_check(sandbox)

    detail = next(s.detail for s in steps if s.name == "configuration")
    assert "(pinned)" in detail and "not pinned yet" not in detail


def test_the_stored_object_is_named_by_the_callers_token(sandbox: Sandbox) -> None:
    _run_check(sandbox, token="b" * 32)

    put = sandbox.calls()[0].split()
    assert put[3] == f"meiva-check/{'b' * 32}.txt"


def test_an_empty_token_is_refused_before_any_storage_call(sandbox: Sandbox) -> None:
    with pytest.raises(ValueError, match="non-empty token"):
        _run_check(sandbox, token="")

    assert sandbox.calls() == []


def test_the_check_never_pins_the_key(sandbox: Sandbox) -> None:
    _run_check(sandbox)

    assert walg_config.pinned_key_id(sandbox.home) is None


def test_a_missing_or_edited_binary_stops_at_the_binary(sandbox: Sandbox) -> None:
    steps = _run_check(sandbox, binary_problem="wal-g is not installed at /x")

    assert _verdicts(steps) == [("configured", True), ("binary", False)]
    assert "meiva converge installs it" in steps[-1].detail
    assert sandbox.calls() == []


def test_an_unusable_configuration_stops_before_any_storage_call(sandbox: Sandbox) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, WALG_PREVENT_WAL_OVERWRITE=None))

    steps = _run_check(sandbox)

    assert _verdicts(steps)[-1] == ("configuration", False)
    assert "WALG_PREVENT_WAL_OVERWRITE" in steps[-1].detail
    assert sandbox.calls() == []


def test_a_key_that_differs_from_the_pinned_one_fails(sandbox: Sandbox) -> None:
    walg_config.load_walg_config(path=sandbox.path, home=sandbox.home)
    sandbox.key_file.write_text("cd" * 32 + "\n")

    steps = _run_check(sandbox)

    assert _verdicts(steps)[-1] == ("configuration", False)
    assert "is not the key this home pinned" in steps[-1].detail
    assert sandbox.calls() == []


def test_unreachable_storage_fails_the_storage_step(sandbox: Sandbox) -> None:
    sandbox.set_mode("fail")

    steps = _run_check(sandbox)

    assert _verdicts(steps)[-1] == ("storage", False)
    assert "simulated failure" in steps[-1].detail


def test_a_credential_that_cannot_delete_fails_even_though_it_can_write(sandbox: Sandbox) -> None:
    sandbox.set_mode("nodelete")

    steps = _run_check(sandbox)

    assert _verdicts(steps)[-1] == ("storage", False)
    assert "cannot delete" in steps[-1].detail and "Delete granted" in steps[-1].detail


def test_a_round_trip_that_returns_other_bytes_fails(sandbox: Sandbox) -> None:
    sandbox.set_mode("corrupt")

    steps = _run_check(sandbox)

    assert _verdicts(steps)[-1] == ("storage", False)
    assert "differs from the one written" in steps[-1].detail
    assert sandbox.stored() == [], "a failed check still cleans up after itself"


def test_postgres_facts_are_reported_not_judged(sandbox: Sandbox) -> None:
    steps = _run_check(sandbox)
    assert steps[-1].ok and "not read" in steps[-1].detail

    with archiving_postgres(sandbox=sandbox) as pg:
        running = _run_check(sandbox, admin=probe.admin_connection_to(pg.target))[-1]

    assert running.ok
    assert "archive_mode=on, matches the configuration" in running.detail
