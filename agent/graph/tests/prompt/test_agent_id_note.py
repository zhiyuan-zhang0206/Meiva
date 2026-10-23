from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import OperationalError

from agent.graph.prompt.context_notes import (
    agent_id_note,
    machine_clause,
    own_label,
    workspace_facts,
)
from agent.graph.prompt.note_text import (
    Workspace,
    agent_id_text,
    label_from_row,
    machine_name_text,
    workspace_display,
)
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.cluster import machine
from base.cluster.machine import MachineIdentity
from base.config.tests.configuration import defaults
from base.db import Database
from base.host.env.agent_slices import AgentSlices


def _machine(reader: Callable[[], str]) -> MachineIdentity:
    return MachineIdentity(
        name=reader,
        role=lambda: frozenset({"agent-runner"}),
        host=lambda: "localhost",
        description=lambda: None,
    )


def _context(
    clients: ClientSet,
    *,
    agent_id: int | None = 29,
    runner: MachineIdentity | None = None,
    workspace: bool = False,
) -> MeivaContext:
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(
            {"workspace_in_system_prompt": workspace}, default_reader=defaults()
        ),
        machine=runner,
        clients=clients,
    )


@pytest.fixture
def labelled_agent(db_conn: psycopg.Connection[Any]) -> None:
    db_conn.execute("INSERT INTO agents (id, label) VALUES (29, '  memory\n  steward ')")
    db_conn.commit()


@pytest.fixture
def clients(database: Database) -> Iterator[ClientSet]:
    owned = ClientSet(database=lambda: database)
    try:
        yield owned
    finally:
        owned.close()


def _content(note: Any) -> str:
    assert note is not None
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.AGENT_ID
    content = str(note.content)
    assert content.startswith("<system_note>\n") and content.endswith("\n</system_note>")
    return content[len("<system_note>\n") : -len("\n</system_note>")]


def test_states_the_id_label_and_machine() -> None:
    text = agent_id_text(29, label="memory steward", machine="runner-a", workspace=None)

    assert text == "Your Agent ID is 29 (label: memory steward, machine: runner-a)."


def test_id_only_when_no_optional_clauses() -> None:
    assert agent_id_text(29, label=None, machine=None, workspace=None) == "Your Agent ID is 29."


def test_partial_clauses_render_alone() -> None:
    assert (
        agent_id_text(29, label="memory steward", machine=None, workspace=None)
        == "Your Agent ID is 29 (label: memory steward)."
    )
    assert (
        agent_id_text(29, label=None, machine="runner-a", workspace=None)
        == "Your Agent ID is 29 (machine: runner-a)."
    )


def test_workspace_clause_carries_the_path_and_the_current_document() -> None:
    waiting = Workspace("/ws/29", "Waiting on a decision", None)
    decided = Workspace("/ws/29", "The decision has arrived", None)

    first = agent_id_text(29, label=None, machine=None, workspace=waiting)
    second = agent_id_text(29, label=None, machine=None, workspace=decided)

    assert first.startswith("Your Agent ID is 29. Your workspace is /ws/29.")
    assert first.endswith("beside your messages:\nWaiting on a decision")
    assert "The decision has arrived" in second
    assert "Waiting on a decision" not in second


def test_workspace_without_a_document_states_only_the_path() -> None:
    bare = Workspace("~/workspaces/29", None, None)

    assert (
        agent_id_text(29, label=None, machine=None, workspace=bare)
        == "Your Agent ID is 29. Your workspace is ~/workspaces/29."
    )


def test_unreadable_document_is_reported_for_repair() -> None:
    broken = Workspace("/ws/29", None, "To user workspace must not be a symlink")

    text = agent_id_text(29, label=None, machine=None, workspace=broken)

    assert text.endswith(
        "\nThe shared context file could not be read: To user workspace must not be a symlink. "
        "Repair it in your workspace."
    )


@pytest.mark.parametrize("row", [None, (None,), ("",)])
def test_label_omits_missing_data(row: tuple[object, ...] | None) -> None:
    assert label_from_row(row) is None


def test_label_normalizes_whitespace() -> None:
    assert label_from_row(("  memory\n  steward ",)) == "memory steward"


def test_machine_name_normalizes_whitespace_and_drops_blank() -> None:
    assert machine_name_text("  runner \n a ") == "runner a"
    assert machine_name_text("   ") is None


def test_workspace_display_is_home_relative_only_under_home() -> None:
    home = Path("/home/zed")

    assert workspace_display(home / ".meiva" / "workspaces" / "29", home) == (
        "~/.meiva/workspaces/29"
    )
    assert workspace_display(Path("/srv/ws/29"), home) == "/srv/ws/29"


def test_workspace_facts_follow_the_document_on_disk(tmp_path: Path) -> None:
    ws = tmp_path / "workspaces" / "29"
    ws.mkdir(parents=True)

    assert workspace_facts(ws, tmp_path) == Workspace("~/workspaces/29", None, None)

    (ws / "to_user.md").write_text("Waiting on a decision")
    assert workspace_facts(ws, tmp_path).document == "Waiting on a decision"

    (ws / "to_user.md").write_text("The decision has arrived")
    assert workspace_facts(ws, tmp_path).document == "The decision has arrived"


def test_workspace_facts_report_a_workspace_that_cannot_be_read(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    facts = workspace_facts(link, tmp_path)

    assert facts.document is None
    assert facts.error == "To user workspace must not be a symlink"


@pytest.mark.usefixtures("labelled_agent")
def test_note_states_the_stored_label_and_the_machine(clients: ClientSet) -> None:
    context = _context(clients, runner=_machine(lambda: "runner-a"))

    note = agent_id_note(context)

    assert _content(note) == "Your Agent ID is 29 (label: memory steward, machine: runner-a)."


@pytest.mark.usefixtures("labelled_agent")
def test_note_states_the_workspace_when_the_section_is_on(
    clients: ClientSet, unit_home: Path
) -> None:
    ws = unit_home / "workspaces" / "29"
    ws.mkdir(parents=True)
    (ws / "to_user.md").write_text("Waiting on a decision")
    context = _context(clients, workspace=True)

    content = _content(agent_id_note(context))

    assert content.startswith("Your Agent ID is 29 (label: memory steward). Your workspace is ")
    assert content.endswith("beside your messages:\nWaiting on a decision")


@pytest.mark.usefixtures("labelled_agent")
def test_note_leaves_the_workspace_out_when_the_section_is_off(
    clients: ClientSet, unit_home: Path
) -> None:
    context = _context(clients, workspace=False)

    assert _content(agent_id_note(context)) == "Your Agent ID is 29 (label: memory steward)."
    assert not (unit_home / "workspaces").exists()


def test_note_renders_under_a_hosted_turn_identity(clients: ClientSet) -> None:
    note = agent_id_note(_context(clients, agent_id=31))

    assert _content(note) == "Your Agent ID is 31."


def test_note_opts_out_without_any_identity() -> None:
    assert agent_id_note(_context(ClientSet(), agent_id=None)) is None


@pytest.mark.usefixtures("labelled_agent")
def test_own_label_reads_the_stored_label(clients: ClientSet) -> None:
    assert own_label(_context(clients), 29) == "memory steward"


def test_own_label_omits_an_agent_without_a_row(clients: ClientSet) -> None:
    assert own_label(_context(clients), 404) is None


def _unreachable() -> Database:
    raise OperationalError("label connection is unavailable")


def test_own_label_propagates_a_connection_failure() -> None:
    context = _context(ClientSet(database=_unreachable))

    with pytest.raises(OperationalError, match="label connection is unavailable"):
        own_label(context, 29)


def test_own_label_propagates_a_context_without_a_database() -> None:
    with pytest.raises(RuntimeError, match="no database handle factory"):
        own_label(_context(ClientSet()), 29)


def test_note_propagates_a_label_failure() -> None:
    with pytest.raises(OperationalError, match="label connection is unavailable"):
        agent_id_note(_context(ClientSet(database=_unreachable)))


def test_machine_clause_states_the_machine_name() -> None:
    context = _context(ClientSet(), runner=_machine(lambda: "runner-a"))

    assert machine_clause(context) == "runner-a"


def test_context_without_a_machine_has_no_machine_clause() -> None:
    assert machine_clause(_context(ClientSet())) is None


def test_unset_machine_name_drops_only_the_machine_clause() -> None:
    def unset() -> str:
        raise machine.MachineNameMissing("unset")

    def broken() -> str:
        raise RuntimeError("settings unreadable")

    assert machine_clause(_context(ClientSet(), runner=_machine(unset))) is None
    with pytest.raises(RuntimeError, match="settings unreadable"):
        machine_clause(_context(ClientSet(), runner=_machine(broken)))
