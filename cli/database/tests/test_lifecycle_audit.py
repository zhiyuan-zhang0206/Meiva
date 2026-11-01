# meiva:note
# The audit rows of an operator lifecycle command, written to the real database.

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.agents.context.clients import DatabaseFactory
from base.events.contract import EVENTS
from base.native_process.root_control.ipc import CallerPayload
from cli.database import OperatorDatabaseFactory
from cli.database.lifecycle_audit import (
    lifecycle_audit,
    operator_lifecycle_audit,
    operator_request_recorder,
    operator_result_recorder,
    record_lifecycle_request,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.database.tests.lifecycle_rows import caller_block


# meiva:note
# The lifecycle rows one operation wrote, by event name.
def audit_rows(db_conn: psycopg.Connection, operation: str) -> dict[str, dict[str, Any]]:
    rows = db_conn.execute(
        "SELECT event_name, agent_id, source, attributes FROM audit_events "
        "WHERE attributes->>'operation_id' = %s",
        (operation,),
    ).fetchall()
    return {
        name: {"agent_id": agent_id, "source": source, "attributes": attributes}
        for name, agent_id, source, attributes in rows
    }


def refuse_database() -> Any:
    raise AssertionError("this command must not reach the database")


# meiva:note
# What launched the command under test, as the scope reads it in production: the parent
# process's command line and the ssh connection it arrived on (None for a local shell).
_SPAWN: dict[str, object] = {
    "parent_argv": ["-zsh", "-c", "/Users/zyonzhang/.meiva/source/.venv/bin/meiva stop"],
    "ssh_connection": "10.1.2.3 51000 10.1.2.4 22",
}


def recorders(
    database: DatabaseFactory, caller: CallerPayload, head_sha: str | None
) -> tuple[Callable[[str, Sequence[str]], None], Any]:
    return (
        operator_request_recorder(
            database_factory=database, caller=caller, head_sha=head_sha, spawn=_SPAWN
        ),
        operator_result_recorder(database_factory=database, caller=caller, head_sha=head_sha),
    )


def test_the_events_are_registered_as_audit() -> None:
    assert EVENTS["lifecycle_request"].category == "audit"
    assert EVENTS["lifecycle_result"].category == "audit"


def test_the_request_and_the_result_name_the_same_version(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> None:
    operation = f"stop@{uuid.uuid4().hex}"
    request, result = recorders(operator_database, caller_block(operation), "39d4614c1")

    with lifecycle_audit(request, result) as command:
        record_lifecycle_request("down", ["gateway", "agent-host"])
        command.exited(0)

    rows = audit_rows(db_conn, operation)
    assert rows["lifecycle_request"]["attributes"]["head_sha"] == "39d4614c1"
    outcome = rows["lifecycle_result"]["attributes"]
    assert outcome["verb"] == "down"
    assert outcome["units"] == ["gateway", "agent-host"]
    assert outcome["outcome"] == "ok"
    assert outcome["head_sha"] == "39d4614c1"
    assert outcome["seconds"] >= 0


def test_the_record_carries_the_wire_caller_block(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> None:
    operation = f"stop@{uuid.uuid4().hex}"
    request, _ = recorders(operator_database, caller_block(operation), None)

    request("down", ["gateway", "frontend"])

    row = audit_rows(db_conn, operation)["lifecycle_request"]
    assert row["source"] == "agent:1818"
    assert row["agent_id"] == 1818
    attributes = row["attributes"]
    assert attributes["verb"] == "down"
    assert attributes["units"] == ["gateway", "frontend"]
    assert attributes["operation_id"] == operation
    assert attributes["head_sha"] is None
    assert attributes["caller"]["machine"] == "macmini"
    assert attributes["caller"]["argv"][-1] == "<redacted>"


def test_the_record_names_what_launched_the_command(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> None:
    operation = f"stop@{uuid.uuid4().hex}"
    request, _ = recorders(operator_database, caller_block(operation), None)

    request("down", ["gateway"])

    attributes = audit_rows(db_conn, operation)["lifecycle_request"]["attributes"]
    assert attributes["spawn"] == _SPAWN


def test_a_human_operator_is_recorded_without_an_agent_id(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> None:
    operation = f"start@{uuid.uuid4().hex}"
    request, _ = recorders(operator_database, caller_block(operation, agent_id=None), "sha")

    request("up", ["gateway"])

    row = audit_rows(db_conn, operation)["lifecycle_request"]
    assert row["source"] == "user"
    assert row["agent_id"] is None


def test_a_failed_command_records_a_failed_outcome(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> None:
    operation = f"start@{uuid.uuid4().hex}"
    request, result = recorders(operator_database, caller_block(operation), "sha")

    with lifecycle_audit(request, result) as command:
        record_lifecycle_request("up", ["gateway"])
        command.exited(1)

    assert audit_rows(db_conn, operation)["lifecycle_result"]["attributes"]["outcome"] == "failed"


def test_a_command_without_a_request_records_no_result(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> None:
    operation = f"start@{uuid.uuid4().hex}"
    _, result = recorders(operator_database, caller_block(operation), "sha")

    with lifecycle_audit(None, result) as command:
        command.exited(0)

    assert audit_rows(db_conn, operation) == {}


def test_an_unreachable_database_never_fails_the_command() -> None:
    def refuse() -> Any:
        raise RuntimeError("settings unavailable")

    request, result = recorders(refuse, caller_block("stop@refused"), "sha")

    with lifecycle_audit(request, result) as command:
        record_lifecycle_request("down", ["gateway"])
        command.exited(0)


# meiva:note
# The sink layer stays best-effort end to end: an operator command whose caller is readable runs
# to completion even when the database factory refuses every call, so a broken recorder cannot
# block the command that repairs the host.
def test_a_sink_that_cannot_be_dialled_never_fails_an_attributable_command(
    command_runtime: CommandRuntime,
) -> None:
    try:
        with operator_lifecycle_audit(
            database_factory=refuse_database,
            environ=command_runtime.config.process_environment(),
            home=command_runtime.home,
            pid=os.getpid(),
        ) as command:
            record_lifecycle_request("down", ["gateway"])
            command.exited(0)
    except Exception as error:
        pytest.fail(
            "a sink that cannot be dialled must never fail an attributable command: the caller "
            "block was readable, so the recorder is the only thing that broke, and the command "
            "that repairs the host cannot depend on it. It raised "
            f"{error!r}"
        )


def test_a_boot_start_is_not_attributed_to_an_operator(command_runtime: CommandRuntime) -> None:
    try:
        with operator_lifecycle_audit(
            database_factory=refuse_database,
            environ=command_runtime.config.process_environment(),
            home=command_runtime.home,
            enabled=False,
            pid=os.getpid(),
        ) as command:
            record_lifecycle_request("up", ["gateway"])
            command.exited(0)
    except Exception as error:
        pytest.fail(
            "a disabled scope must never dial the recorder: a boot start has no caller to "
            "attribute, so the factory that refuses every call cannot fail it. It raised "
            f"{error!r}"
        )


def test_an_operator_command_audits_itself_as_the_process_that_runs_it(
    operator_database: OperatorDatabaseFactory,
    db_conn: psycopg.Connection,
    command_runtime: CommandRuntime,
) -> None:
    with operator_lifecycle_audit(
        database_factory=operator_database,
        environ=command_runtime.config.process_environment(),
        home=command_runtime.home,
        pid=os.getpid(),
    ) as command:
        record_lifecycle_request("down", ["gateway"])
        command.exited(0)

    rows = db_conn.execute(
        "SELECT event_name, attributes->'caller'->>'pid', attributes->>'verb', "
        "attributes->'head_sha' FROM audit_events "
        "WHERE event_name IN ('lifecycle_request', 'lifecycle_result') ORDER BY event_name"
    ).fetchall()
    assert [(name, verb) for name, _pid, verb, _sha in rows] == [
        ("lifecycle_request", "down"),
        ("lifecycle_result", "down"),
    ]
    assert rows[0][1] == str(os.getpid())
    # meiva:note
    # This home has no source checkout, which is a null commit and not a refused command.
    assert [sha for *_rest, sha in rows] == [None, None]


_CHILD = Path(__file__).with_name("child_audit_without_cwd.py")


# meiva:note
# Run `child_audit_without_cwd.py` in a fresh interpreter, as a shell that lost its working
# directory runs a command.
def _run_without_cwd(scope: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path / "home")
    # meiva:note
    # The child is this repository's own fixed probe script, run with the interpreter in
    # isolated mode.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(_CHILD), str(tmp_path / "gone"), scope],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# meiva:note
# The caller block refuses the command: it is the only fact that says who asked, and an operator
# can destroy it on purpose (a removed working directory), so accepting the failure would hand
# out one unattributed execution per `rm -rf`. The sink is never dialed and the body never runs.
# Ruling: memory pool `meiva/rulings/prod/audit-identity-unreadable-must-refuse-20261031.md`.
def test_a_command_that_cannot_name_its_caller_is_refused(tmp_path: Path) -> None:
    child = _run_without_cwd("operator", tmp_path)

    assert child.returncode == 3, (
        "a lifecycle command that cannot read its caller block must exit 3 rather than run: "
        "that block is the only fact naming who asked, and an operator can remove it on "
        "purpose (a deleted working directory), so proceeding hands out one unattributed "
        f"execution per `rm -rf`. stderr={child.stderr!r}"
    )
    assert "command ran" not in child.stdout, (
        "the command body must not run once the caller cannot be named: the refusal exists to "
        "stop an unattributed lifecycle execution, not to record one after it happened. "
        f"stdout={child.stdout!r}"
    )
    [refusal] = child.stdout.splitlines()
    assert refusal.startswith("refused: lifecycle audit cannot read the caller block"), (
        "the one line a refused command prints must say what was missing and that nothing ran, "
        f"so an operator who broke their shell learns why. stdout={child.stdout!r}"
    )
    assert refusal.endswith("rerun from a working directory"), (
        "the refusal must carry the recovery step: the caller block is unreadable because this "
        f"process has no working directory. stdout={child.stdout!r}"
    )


def test_a_boot_start_needs_no_caller_and_says_nothing_when_it_cannot_be_named(
    tmp_path: Path,
) -> None:
    child = _run_without_cwd("boot", tmp_path)

    assert child.returncode == 0, (
        "a boot start must still run without a caller block: it is the one lifecycle entry no "
        "operator asked for, so refusing it would make a machine that lost its working directory "
        f"unable to start. stderr={child.stderr!r}"
    )
    assert child.stdout.splitlines() == ["command ran"], (
        "the boot start runs its body and prints only that, rather than a refusal it does not "
        f"owe. stdout={child.stdout!r}"
    )
    assert child.stderr == "", (
        "a boot start that cannot name a caller says nothing: there is no operator to warn and "
        f"no audit row it could not write. stderr={child.stderr!r}"
    )


def test_the_outcome_is_recorded_once() -> None:
    with lifecycle_audit(None) as command:
        command.exited(0)
        with pytest.raises(RuntimeError, match="recorded twice"):
            command.exited(0)


# meiva:note
# A command scope's recorder: the batches it was handed.
class Requests:
    def __init__(self) -> None:
        self.batches: list[tuple[str, list[str]]] = []

    def __call__(self, verb: str, units: Sequence[str]) -> None:
        self.batches.append((verb, list(units)))


def test_a_command_scope_records_and_restores_the_previous_recorder() -> None:
    outer, inner = Requests(), Requests()

    with lifecycle_audit(outer):
        with lifecycle_audit(inner):
            record_lifecycle_request("down", ["gateway"])
        record_lifecycle_request("up", ["gateway"])
    record_lifecycle_request("down", ["gateway"])

    assert inner.batches == [("down", ["gateway"])]
    assert outer.batches == [("up", ["gateway"])]


def test_outside_a_command_no_request_is_recorded() -> None:
    requests = Requests()

    with lifecycle_audit(requests):
        pass
    record_lifecycle_request("down", ["gateway"])

    assert requests.batches == []
