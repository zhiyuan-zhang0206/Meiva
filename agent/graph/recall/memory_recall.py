"""Passive memory recall: content-triggered injection of memory-pool notes.

Where `memory_index_note` (this plugin's `notes.py`) keeps the standing index
(MEMORY.md) permanently in front of the agent, passive recall reaches into the
*rest* of the pool: before a turn woken by new inbound, it searches the pool with
the recent conversation as the query and injects the notes the gateway's ranking
scores at or above `memory_recall_inject_threshold`, as a system-styled note.
The search is the same one `meiva.memory.search` runs -- vector retrieval of the
best chunk of each candidate note, then a decision model that scores every chunk's
relevance to the conversation -- so recall holds no model call of its own and no
relevance judgement of its own. Each injected note carries its pool-relative
path, its frontmatter description (empty when absent, never synthesized from
title/body), its score and the passage that matched. The agent sees relevant
durable notes surface on their own, without having to call `meiva.memory.search`.

A removable layer: gated by the agent's `passive_memory_recall_enabled` slice field (default
on), and a no-op whenever the search does not come back with results -- index
down, ranking model down, gateway erroring, feature off -- so the call site degrades
to nothing rather than failing the turn (the next turn tries again). It runs before
the LLM on every inbound turn, so a search failure that escaped would kill the agent
process, not just the recall. Same-window dedup is the caller's job (it passes the
paths injected in the current context window in and persists the ones this call added); the search leaves them out.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection
from pathlib import Path
from typing import NamedTuple

from langchain_core.messages import AnyMessage, HumanMessage

from agent.graph.recall.conversation_query import build_conversation_query
from agent.graph.recall.memory_note import SearchHit, qualifying, render_note_text
from agent.messages import NoteTag, system_note_message
from base.agents.context import MeivaContext
from base.agents.context.gateway_client import memory_search as memory_client
from base.api_contracts.agent_contract import GatewayUnavailable, IndexerUnavailable
from base.host.env.agent_slices import AgentSlices
from base.log import logger


class PassiveRecall(NamedTuple):
    """Result of a passive recall pass.

    note: the system-styled human message to inject.
    paths: the memory-pool-relative note paths it lists -- the caller records
        these so the same note is not injected again this session.
    """

    note: HumanMessage
    paths: set[str]


async def _search_or_none(
    query: str, k: int, *, exclude: Collection[str], deadline: float, ctx: MeivaContext
) -> list[memory_client.MemorySearchResult] | None:
    """The `k` best results outside `exclude`, or None when the search is unavailable or failed.

    `deadline` is the pass's whole budget and travels in the request, so the gateway stops
    ranking when the pass gives up. The blocking call cannot be interrupted from here, and
    `attempts=1` keeps it from starting a second search after the pass has moved on: the next
    turn is the retry.
    """
    import httpx  # deferred: stays off the child boot path

    try:
        return await asyncio.to_thread(
            memory_client.memory_search,
            query,
            k,
            purpose="recall",
            exclude=exclude,
            timeout=deadline,
            attempts=1,
            context=ctx,
        )
    except (GatewayUnavailable, IndexerUnavailable) as exc:
        # Recall is an enhancement; a memory-index outage must not crash the
        # turn. Skip this turn and let the next one retry. Debug level because
        # the gateway said this in the wire contract -- a restart or a stalled
        # embedder is a modelled, self-clearing state, not news.
        logger.debug(
            "[{label}] {body}",
            label="passive-recall",
            body=f"memory search unavailable: {exc!r}",
            event="passive_recall",
        )
        return None
    except httpx.HTTPStatusError as exc:
        # The endpoint failed in a way it does not model: a status whose body
        # carries no wire `reason`, so `raise_from_response` re-raises the raw
        # HTTP error to fail fast. Fail-fast is right for a call the agent made
        # on purpose, but recall runs before the LLM on every inbound turn, so
        # here it took the whole process down with it -- one intermittent 500
        # killed agent 405 on 2026-08-07. Degrade to no recall, at error level:
        # unlike the branch above, nobody designed this response, so it is a
        # gateway bug someone has to see.
        #
        # Only the status-code half of `httpx.HTTPError` is caught. Its
        # transport half never reaches here: `base.agents.context.gateway_client.transport.post` retries
        # those and converts them to `GatewayUnavailable`, which the branch
        # above already takes.
        logger.error(
            "[{label}] {body}",
            label="passive-recall",
            body=(
                f"memory search failed with HTTP {exc.response.status_code} "
                f"at {exc.request.url}; continuing with no recall this turn"
            ),
            event="passive_recall",
        )
        return None


async def passive_memory_recall(
    messages: Collection[AnyMessage],
    *,
    agent: AgentSlices,
    context: MeivaContext,
    memory_root: Path,
    injected_paths: Collection[str] = frozenset(),
) -> PassiveRecall | None:
    """Search the memory pool on the recent conversation and render the notes that
    clear the score threshold as a note to inject, or `None` when there is nothing
    to add.

    `None` when: the feature is disabled, the conversation yields no query, the
    search failed (index or ranking model unavailable, or the gateway answered with
    an error status), or no match both reaches `memory_recall_inject_threshold`
    and is new to this window and present on this machine. `injected_paths` and the
    returned `PassiveRecall.paths` are memory-pool-relative.

    Never raises on a failed search: the caller is a before_llm hook, so an
    exception here ends the agent process rather than the recall.
    """
    memory = agent.memory
    if agent.sandbox.eval_isolation or not memory.passive_memory_recall_enabled:
        return None
    if memory.memory_recall_inject_max == 0:
        return None
    query = build_conversation_query(messages)
    if not query:
        return None
    # Notes injected earlier in this context window are excluded from the search itself: they neither
    # take a result slot nor cost a ranking request, so one pass costs the same at any age.
    search_started = time.monotonic()
    results = await _search_or_none(
        query,
        memory.memory_recall_inject_max,
        exclude=injected_paths,
        deadline=memory.memory_recall_deadline_seconds,
        ctx=context,
    )
    if results is None:
        return None
    search_ms = round((time.monotonic() - search_started) * 1000)

    picked = qualifying(
        [SearchHit(item.path, item.description, item.score, item.chunk) for item in results],
        threshold=memory.memory_recall_inject_threshold,
        limit=memory.memory_recall_inject_max,
        present={item.path for item in results if (memory_root / item.path).is_file()},
    )
    # The search leg is the one a congested gateway stretches (a fleet wake queues
    # searches behind the search endpoint's semaphore) and it now includes the ranking
    # model's call; it sits in front of the turn's first LLM.
    logger.info(
        "[{label}] {body}",
        label="passive-recall",
        body=f"search {search_ms}ms, {len(results)} result(s) -> {len(picked)} injected",
        event="passive_recall",
        search_ms=search_ms,
        injected=len(picked),
    )
    if not picked:
        return None

    note = system_note_message(
        content=render_note_text(picked),
        tag=NoteTag.MEMORY,
        created_at=context.require_clock().now(),
    )
    return PassiveRecall(note=note, paths={item.path for item in picked})
