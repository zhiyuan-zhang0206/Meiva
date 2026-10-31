"""Typed gateway operations for the process-local SDK.

The SDK resolves its existing process context; the base gateway client owns
HTTP, retry policy and wire-error translation for explicit contexts.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Literal, assert_never
from uuid import UUID

import meiva.sdk_surface.agent_identity
from base.agents.context import MeivaContext
from base.agents.context.gateway_client import launch_retry as _launch_retry
from base.agents.context.gateway_client import transport as _transport
from base.api_contracts.agent_contract import GatewayUnavailable as GatewayUnavailable
from base.log import logger
from meiva.gateway_client import agent_plans, plans
from meiva.gateway_client.plans import Request
from meiva.sdk_surface import process_context


def _ctx(context: MeivaContext | None) -> MeivaContext:
    return context if context is not None else process_context.current_context()


# meiva:note
# Runs one request plan through the base transport and checks the answer against the wire
# contract; the plan's `None` timeout and retry count leave the context's own configuration in
# charge. httpx is imported here so `import meiva` does not load it.
def _run(plan: Request, context: MeivaContext) -> _transport.TransportResponse:
    timeout = None
    if plan.timeout_seconds is not None:
        import httpx

        timeout = httpx.Timeout(plan.timeout_seconds)
    match plan.method:
        case "GET":
            response = _transport.get(
                plan.path,
                params=plan.params,
                timeout=timeout,
                max_retries=plan.max_retries,
                context=context,
            )
        case "PATCH":
            response = _transport.patch(plan.path, json=plan.body, context=context)
        case "POST":
            response = _transport.post(
                plan.path,
                plan.body,
                timeout=timeout,
                idempotency_key=plan.idempotency_key,
                max_retries=plan.max_retries,
                context=context,
            )
        case _:
            assert_never(plan.method)
    _transport.raise_from_response(response)
    return response


def _actor_or(source: str | None) -> str:
    return source if source is not None else meiva.sdk_surface.agent_identity.require_actor()


def get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: _transport.TransportTimeout | None = None,
    max_retries: int | None = None,
    context: MeivaContext | None = None,
) -> _transport.TransportResponse:
    """Read a gateway route using an explicit context or the SDK process context.

    Keep the context's HTTP client, timeout and bounded retry policy. A shared
    host must supply its context; it cannot borrow an ambient agent identity.
    """
    return _transport.get(
        path, params=params, timeout=timeout, max_retries=max_retries, context=_ctx(context)
    )


# meiva:note
# `use_client` accepts any HTTP client test double or `httpx.Client` and returns it unchanged so
# callers keep the concrete type of the client they passed into the context manager.
# meiva:link meiva/agents/docs/agents.meiva.okf.md
@contextmanager
def use_client[T](client: T) -> Generator[T]:
    """Route every SDK call in the block through *client* — a FastAPI `TestClient` for an
    in-process gateway, or any `httpx.Client` — then put back whichever client was there before."""
    bound = process_context.current_context()

    def _builder(_url: str) -> T:
        return client

    derived = replace(
        bound,
        clients=bound.clients.with_gateway(_builder),  # pyright: ignore[reportArgumentType]
    )
    process_context.bind_context(derived)
    try:
        yield client
    finally:
        process_context.bind_context(bound)


def get_launch_attempt(agent_id: int) -> UUID:
    """Read the current metadata attempt; an absent old field is not evidence."""
    _launch_retry.validate_agent_id(agent_id, operation="launch attempt observation")
    return _launch_retry.get_launch_attempt(agent_id, context=_ctx(None))


def retry_launch(
    agent_id: int,
    *,
    idempotency_key: str,
    expected_prior_attempt_id: str | UUID,
) -> int:
    """Retry one observed attempt through the fixed guarded operation."""
    _launch_retry.validate_retry_admission(key=idempotency_key, prior=expected_prior_attempt_id)
    _launch_retry.validate_agent_id(agent_id, operation="guarded launch retry")
    return _launch_retry.retry_launch(
        agent_id,
        idempotency_key=idempotency_key,
        expected_prior_attempt_id=expected_prior_attempt_id,
        context=_ctx(None),
    )


def spawn(
    *,
    spawner: str,
    prompt: str | None,
    fork_from: int | None,
    prompt_source: str,
    machine: str | None = None,
    config: dict[str, object] | None = None,
    label: str | None = None,
    harness: str | None = None,
    idempotency_key: str,
) -> int:
    """Create or fork an agent with a caller-owned immutable identity."""
    plan = plans.spawn_request(
        plans.SpawnIntent(
            spawner=spawner,
            prompt=prompt,
            fork_from=fork_from,
            prompt_source=prompt_source,
            machine=machine,
            config=config,
            label=label,
            harness=harness,
        ),
        idempotency_key,
    )
    receipt = plans.spawn_receipt(_transport.json_object(_run(plan, _ctx(None))))
    if receipt.withdrawn_model is not None:
        # The spawner-visible receipt for a withdrawal settlement (task #4306):
        # the gateway stored the registered fallback, but this template still
        # names the withdrawn id — fix it at the source.
        logger.warning(
            "spawn config_overlay llm_model {requested!r} is withdrawn; the "
            "agent was stored on the registered fallback {resolved!r} — update "
            "the template (task #4306)",
            event="spawn_config_normalized",
            requested=receipt.withdrawn_model[0],
            resolved=receipt.withdrawn_model[1],
        )
    return receipt.agent_id


def send_message(
    agent_id: int,
    *,
    content: str | list[dict[str, object]],
    source: str,
    completion_notice: bool = False,
) -> None:
    """Deliver with bounded same-key HTTP retries; failures return to the caller.

    The gateway's durable inbound key prevents duplicate acceptance after response
    loss. There is no sender journal or delivery after this invocation exits.
    """
    plan = plans.message_request(
        agent_id,
        content=content,
        source=source,
        completion_notice=completion_notice,
        idempotency_key=str(uuid.uuid4()),
    )
    _run(plan, _ctx(None))


def send_system_note(
    agent_id: int,
    *,
    content: str,
    note_tag: str,
    source: str,
    task_id: int | None,
    resurrect: bool,
    idempotency_key: str,
) -> int:
    """POST /api/agents/{id}/system-note — deliver a framework system note.

    Returns the durable inbound id; renders as a system note (no sender
    prefix), waking a live agent, reviving a terminated one when `resurrect`
    is set (same 120 s per-call timeout as send_message).
    """
    note = plans.NoteIntent(
        content=content, note_tag=note_tag, source=source, task_id=task_id, resurrect=resurrect
    )
    plan = plans.system_note_request(agent_id, note, idempotency_key)
    return int(_transport.json_object(_run(plan, _ctx(None)))["inbound_id"])


def get_last_message(agent_id: int, caller: str) -> str | None:
    """GET /api/agents/{id}/last-message → the agent's most recent AI turn text."""
    plan = Request("GET", f"/api/agents/{agent_id}/last-message", params={"caller": caller})
    return _transport.json_object(_run(plan, _ctx(None)))["text"]


def get_neighbors(agent_id: int, *, depth: int, limit: int) -> list[dict[str, Any]]:
    """GET /api/agents/{id}/neighbors → list of neighbor dicts.

    Each dict has id / label / status / depth / score, strongest first
    (order preserved from the gateway).
    """
    plan = Request(
        "GET", f"/api/agents/{agent_id}/neighbors", params={"depth": depth, "limit": limit}
    )
    return _transport.json_object(_run(plan, _ctx(None)))["neighbors"]


def get_ancestors(agent_id: int) -> list[dict[str, Any]]:
    """GET /api/agents/{id}/neighbors → the `ancestors` rows: the spawn/fork
    chain above `agent_id`, nearest ancestor first (the gateway walks to the
    top, so the neighbors `depth`/`limit` params do not apply). Same dict
    shape as get_neighbors."""
    plan = Request("GET", f"/api/agents/{agent_id}/neighbors", params={"depth": 1, "limit": 20})
    return _transport.json_object(_run(plan, _ctx(None)))["ancestors"]


# The born-chain read runs at every context establishment, on the agent
# host's event loop. One attempt with a tight bound is deliberate: a gateway
# hiccup skips the inherited note for this window (the next establishment
# retries — the chain is immutable, so nothing is lost) instead of parking
# the agent's birth behind seconds of retry backoff it cannot use.
_BORN_CHAIN_TIMEOUT_S = 5.0


def get_born_chain(agent_id: int, *, context: MeivaContext | None = None) -> list[dict[str, Any]]:
    """GET /api/agents/{id}/born-chain → the `ancestors` rows: the immutable
    birth chain above `agent_id`, nearest ancestor first (1 = direct birth
    parent). Each dict carries agent_id / label / status / machine / depth.

    Unlike `get_ancestors`, this route reads no tie graph — no neighbor
    ranking, no Loki live tail — so turn-path callers (the inherited-memory
    context note at window establishment) can resolve the chain cheaply.
    The call itself is one-shot with a tight timeout (see
    `_BORN_CHAIN_TIMEOUT_S`); callers degrade on `GatewayUnavailable`."""
    plan = Request(
        "GET",
        f"/api/agents/{agent_id}/born-chain",
        timeout_seconds=_BORN_CHAIN_TIMEOUT_S,
        max_retries=1,
    )
    return _transport.json_object(_run(plan, _ctx(context)))["ancestors"]


def list_agents(
    *,
    scope: str = "live",
    query: str = "",
    before_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read one bounded directory page; the caller owns cursor traversal."""
    plan = plans.list_agents_request(scope=scope, query=query, before_id=before_id, limit=limit)
    return _transport.json_object(_run(plan, _ctx(None)))


def patch_label(agent_id: int, label: str, *, source: Literal["user", "self"] = "self") -> None:
    """Set an agent's label through the gateway; `""` resets it to unset.

    The label sticks (the background labeler no longer renames it), the change is
    audited as `label_change` with `source`, and every client's view refreshes live.
    """
    _run(plans.patch_label_request(agent_id, label, source), _ctx(None))


def get_agent(agent_id: int) -> dict[str, Any]:
    """Read an agent directly, independently of directory scope or pagination."""
    return _transport.json_object(_run(Request("GET", f"/api/agents/{agent_id}"), _ctx(None)))


def terminate(
    agent_id: int,
    *,
    source: str | None = None,
    message: str | None = None,
    force: bool = False,
    kill_all_shell_sessions: bool = False,
) -> dict[str, Any]:
    """POST /api/agents/{id}/terminate → response dict.

    `status` is "enqueued" / "already_terminated"; `open_tasks` carries what
    the agent still owned as it went down, if anything; `shell_sessions`
    carries what a requested shell-session kill did.

    An omitted source requires the established actor identity. Callers without
    an agent or system identity must supply an explicit source.

    force=True requests interruption. Hosted force returns "enqueued" while
    the original host drains actual work; acceptance is not observed exit.

    message, when present, is retained for the agent's next resurrection while
    termination proceeds without waiting for another response.

    kill_all_shell_sessions=True also kills every shell session the agent owns
    on its home machine. Sent only when set.
    """
    plan = agent_plans.terminate_request(
        agent_id,
        source=_actor_or(source),
        message=message,
        force=force,
        kill_all_shell_sessions=kill_all_shell_sessions,
    )
    return _transport.json_object(_run(plan, _ctx(None)))


def restart(
    agent_id: int,
    *,
    source: str | None = None,
    message: str | None = None,
) -> str:
    """POST /api/agents/{id}/restart → status string.

    An omitted source requires the established actor identity. Callers without
    an agent or system identity must supply an explicit source.
    """
    plan = agent_plans.restart_request(agent_id, _actor_or(source), message=message)
    return _transport.json_object(_run(plan, _ctx(None)))["status"]


# meiva:note
# POST /api/agents/{id}/cancel → status string (`cancelled` / `not_running`).
def cancel(agent_id: int) -> str:
    response = _run(Request("POST", f"/api/agents/{agent_id}/cancel", body={}), _ctx(None))
    return _transport.json_object(response)["status"]


# meiva:note
# POST /api/agents/{id}/compact → the queued `compact_request` inbound id.
def compact(agent_id: int) -> int:
    response = _run(Request("POST", f"/api/agents/{agent_id}/compact", body={}), _ctx(None))
    return _transport.json_object(response)["inbound_id"]


def resurrect(agent_id: int, *, prompt: str, resurrected_by: str | None = None) -> str:
    """POST /api/agents/{id}/resurrect -> status string.

    An omitted resurrected_by requires the established actor identity. Callers
    without an agent or system identity must supply explicit provenance.

    prompt is required -- a resurrected agent needs to know why it
    was woken up and what to do.
    """
    plan = agent_plans.resurrect_request(agent_id, prompt, _actor_or(resurrected_by))
    return _transport.json_object(_run(plan, _ctx(None)))["status"]


def register_page(
    agent_id: int,
    *,
    name: str,
    port: int,
    host: str,
    title: str | None,
    serve_dir: str | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """POST /api/keyed/v1/agents/{id}/pages → PageRow dict (id/name/port/title/...).

    One key per call: transport-level retries share it, so the gateway
    replays the original acceptance instead of replacing a newer page.

    `serve_dir`: directory the page server serves — recorded so agent boot
    can re-serve a dead page server after resurrect/restart. Only
    serve() sets it; show() leaves it None.
    """
    page = agent_plans.PageIntent(
        name=name, port=port, host=host, title=title, serve_dir=serve_dir, ttl_seconds=ttl_seconds
    )
    plan = agent_plans.register_page_request(agent_id, page, uuid.uuid4().hex)
    return _transport.json_object(_run(plan, _ctx(None)))


def close_page(agent_id: int, name: str) -> bool:
    """Close the agent's open page `name`; False when no such page is open.

    Observes the open page list first, then closes exactly that registry row
    through POST /api/keyed/v1/agents/{id}/pages/{name}/close. A registration
    that replaces the observed row in between fails the close with 409
    (httpx.HTTPStatusError) instead of closing the newer page.
    """
    observed = agent_plans.open_page(list_open_pages(agent_id), name)
    if observed is None:
        return False
    _run(agent_plans.close_page_request(agent_id, observed, uuid.uuid4().hex), _ctx(None))
    return True


def list_open_pages(agent_id: int) -> list[dict[str, Any]]:
    """GET /api/agents/{id}/pages → the agent's open PageRow dicts."""
    return _transport.json_rows(_run(Request("GET", f"/api/agents/{agent_id}/pages"), _ctx(None)))


def list_machines() -> list[dict[str, Any]]:
    """GET /api/cluster/machines → list of {name, description, live} dicts."""
    return _transport.json_rows(_run(Request("GET", "/api/cluster/machines"), _ctx(None)))
