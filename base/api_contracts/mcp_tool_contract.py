from __future__ import annotations

# The SDK uses the explicit description verbatim, including indentation and
# trailing whitespace.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_agents": """Read one agent directory page, newest IDs first.

        `live` includes all nonterminated agents; use `terminated` for history
        or `all` for both. `query` matches a label substring or an exact agent
        ID. Returns `agents` and `next_cursor`: pass that cursor as `before_id`
        with the same filters to continue. None means no further results.
        Each call reads at most `limit` agents (1 through 200).
        """,
    "get_agent": """Read the full state of one agent by id.

        Includes lifecycle details, what the agent is doing right now, and any
        questions it is blocked on waiting for an answer — answer those with `send_message`.
        """,
    "spawn_agent_guarded_v1": """Create an agent with a retained original birth receipt.

        Requires a 1-128 character idempotency_key. Reuse the same key and
        arguments after response loss; changed requests conflict. A new key creates a new agent.
        Replay retains the original agent, placement, config and launch attempt.
        It can wake only the original pending, unadmitted attempt; historical
        acceptance does not prove execution or revive later work.
        Supply a standing objective in prompt. label names the agent in fleet
        views; machine selects its host. config_overlay overrides its settings.
        prompt_source (default `user`) and spawner (default `mcp`) are declared
        by you and validated by the API.
        No automatic retries are added.
        """,
    "send_message": """Send a message to a running agent — a new instruction, more context,
        or the answer to a question it is blocked on.

        The message is queued and picked up when the agent finishes its
        current step, so this returns before the agent has read it; it does
        not return the agent's reply. Read the reply with `get_messages`.
        Messaging an agent that has already terminated brings it back with its
        history intact.

        `source` declares who the message is from and is passed to the API
        unchanged, which validates it (see the legal sources in
        `base/agents/messages/envelope.py:validate_source`). The default `user`
        delivers it as the user; `external_agent:<subject>[:<instance>]`
        delivers it as a message from that external agent, for when you
        collaborate with the agent beside the user rather than speaking for the
        user. A source is a self-declared label and adds no permissions.

        An optional idempotency_key identifies a retry of this exact message.
        """,
    "get_messages": """Read an agent's conversation history — what it was told and what it
        has said and done.

        Returns the newest `limit` messages (the API's default window when
        omitted), oldest first, as the raw LangChain message dumps (`type`,
        `content`, `tool_calls`, ...). `msg_count` is the full history length
        and `has_more` says whether older messages exist.
        """,
    "terminate_agent": """DESTRUCTIVE. End an agent: it stops working and its process exits.

        The agent finishes its current step first, so work in flight is not
        cut off mid-way. `force=True` requests interruption instead; an
        `enqueued` result means accepted, not that the agent or its owned work
        has exited. Use force only when a clean stop cannot progress.

        The agent's history survives either way, and `send_message` revives
        it, so this is reversible; it is destructive in that it stops running
        work. `message` saves a final instruction for that later revival
        without asking the agent to respond before exiting. The result also
        reports `open_tasks` — the tasks the agent still owns as it goes down
        (at most five, most recently updated first; null when it owns none).
        """,
    "cluster_status": """Report the health of the Meiva cluster itself — which host answered,
        what it is capable of running, and whether it is paused.

        A paused cluster is mid-maintenance: agents are stopped and spawns
        will not run until it resumes. Check this first when the agent tools
        start failing.
        """,
}


_SERVER_INSTRUCTIONS = (
    "\n"
    "Meiva runs a fleet of long-lived autonomous agents. An agent is a persistent\n"
    "process with its own conversation history that keeps working after you stop\n"
    "talking to it — not a request/response endpoint.\n"
    "\n"
    "The normal loop: `spawn_agent_guarded_v1` with a goal and a caller key,\n"
    "then `get_agent` / `get_messages` to watch it work. Creation returns before\n"
    "the agent has done anything. Use\n"
    "`send_message` to steer or answer it, `terminate_agent` when it is done. Because\n"
    "agents work asynchronously, a transcript read right after a spawn is usually\n"
    "still empty; poll rather than assume failure.\n"
    "\n"
    "Every tool here acts on one cluster — the one this gateway belongs to.\n"
    "There is no cluster argument."
)


def server_instructions() -> str:
    """The fleet instructions the gateway advertises on MCP initialize."""
    return _SERVER_INSTRUCTIONS


def tool_description(name: str) -> str:
    """The advertised prose of one control tool."""
    return _TOOL_DESCRIPTIONS[name]
