# meiva:note
# What a draft endpoint left behind: the agent it spawned, as the database holds it.
#
# A draft route (`guide`, `packages`, `schedules`) spawns a purpose-built agent through the
# guarded creation path. The tests run that path for real and read the committed row and its
# first prompt back, instead of intercepting the call that creates the agent.

from __future__ import annotations

from dataclasses import dataclass

import psycopg

__all__ = ["DraftedAgent", "drafted_agent"]


@dataclass(frozen=True)
class DraftedAgent:
    label: str | None
    spawner: str
    prompts: list[tuple[str, str]]

    # meiva:note
    # The one first prompt the draft route wrote.
    @property
    def prompt(self) -> str:
        ((content, _source),) = self.prompts
        return content


def drafted_agent(conn: psycopg.Connection, agent_id: int) -> DraftedAgent:
    row = conn.execute(
        "SELECT a.label, m.spawner FROM agents a JOIN agents_meta m USING (id) WHERE a.id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None, f"no agent {agent_id}"
    prompts = conn.execute(
        "SELECT content, source FROM inbound_messages WHERE agent_id = %s AND kind = 'chat' "
        "ORDER BY id",
        (agent_id,),
    ).fetchall()
    return DraftedAgent(label=row[0], spawner=row[1], prompts=prompts)
