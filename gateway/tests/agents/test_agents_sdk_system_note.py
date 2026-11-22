"""SDK system notes retain agent attribution and explicit tags through actual Gateway routes."""

from uuid import uuid4

import psycopg
import pytest

import meiva
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from gateway.tests.agents.sdk_support import (
    sdk_via_gateway as sdk_via_gateway,
)
from gateway.tests.agents.sdk_support import spawn_agent
from meiva.agents import AgentNotFound
from meiva.tests.fixtures.pin_agent import pin_agent


class TestSendSystemNote:
    def test_send_system_note_inserts_system_note_inbound_with_explicit_tag(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """send_system_note posts a kind='system_note' inbound (agent source +
        explicit note tag) — never a peer chat row."""
        pin_agent(
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        )
        peer_id = meiva.agents.spawn()

        inbound_id = meiva.agents.send_system_note(
            peer_id,
            "Context was refreshed.",
            tag="context",
            idempotency_key=str(uuid4()),
        )
        assert isinstance(inbound_id, int)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, payload FROM inbound_messages WHERE agent_id = %s",
                (peer_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        content, kind, source, payload = rows[0]
        assert kind == "system_note"
        assert source == f"agent:{meiva.self.AGENT_ID}"
        assert content == "Context was refreshed."
        assert payload == {"note_tag": "context", "delivery_resurrect": True}

    def test_send_system_note_to_terminated_is_fine(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A note with resurrect=True reaches a terminated
        agent — auto-resurrect is the gateway delivery detail, the SDK just
        posts the note and returns its id."""
        pin_agent(
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        )
        peer_id = meiva.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (peer_id,))
        db_conn.commit()

        inbound_id = meiva.agents.send_system_note(
            peer_id, "Context was refreshed.", tag="context", idempotency_key=str(uuid4())
        )
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            kinds = [row[0] for row in cur.fetchall()]
        assert "system_note" in kinds

    def test_send_system_note_normalizes_tuple_content(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Same trailing-comma class as send_message — a one-element tuple
        unwraps to the note text instead of 422ing the gateway."""
        pin_agent(
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        )
        peer_id = meiva.agents.spawn()

        # Runtime value of `("Context was " "refreshed.",)`: implicit
        # concatenation plus trailing comma.
        content: object = ("Context was refreshed.",)
        inbound_id = meiva.agents.send_system_note(
            peer_id, content, tag="context", idempotency_key=str(uuid4())
        )  # pyright: ignore[reportArgumentType]
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Context was refreshed."

    def test_send_system_note_rejects_multi_element_content(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A multi-element tuple is a coding mistake — TypeError, never joined."""
        pin_agent(
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        )
        peer_id = meiva.agents.spawn()

        content: object = ("Context was ", "refreshed.")
        with pytest.raises(TypeError, match="content must be a string"):
            meiva.agents.send_system_note(
                peer_id, content, tag="context", idempotency_key=str(uuid4())
            )  # pyright: ignore[reportArgumentType]

    def test_send_system_note_to_nonexistent_raises(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        )
        with pytest.raises(AgentNotFound):
            meiva.agents.send_system_note(
                9999, "ghost", tag="context", idempotency_key=str(uuid4())
            )
