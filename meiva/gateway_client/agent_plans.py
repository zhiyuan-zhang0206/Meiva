# meiva:note
# The lifecycle and page requests the SDK sends for one agent, as values: the same contract as
# `meiva/gateway_client/plans.py`. The actor a call acts as and the idempotency keys are minted by
# the facade and arrive here as strings. `open_page` is the one decision that reads a gateway
# answer: a page is closed only by the registry row the caller observed, so the plan names that
# row's id.
# meiva:link meiva/agents/docs/agents.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from meiva.gateway_client.plans import Request

__all__ = [
    "PageIntent",
    "close_page_request",
    "open_page",
    "register_page_request",
    "restart_request",
    "resurrect_request",
    "terminate_request",
]


@dataclass(frozen=True, slots=True)
class PageIntent:
    name: str
    port: int
    host: str
    title: str | None
    serve_dir: str | None
    ttl_seconds: int | None


def terminate_request(
    agent_id: int,
    *,
    source: str,
    message: str | None,
    force: bool,
    kill_all_shell_sessions: bool,
) -> Request:
    body: dict[str, Any] = {"source": source}
    if message is not None:
        body["message"] = message
    if force:
        body["force"] = True
    if kill_all_shell_sessions:
        body["kill_all_shell_sessions"] = True
    return Request("POST", f"/api/agents/{agent_id}/terminate", body=body)


def restart_request(agent_id: int, source: str) -> Request:
    return Request("POST", f"/api/agents/{agent_id}/restart", body={"source": source})


def resurrect_request(agent_id: int, prompt: str, resurrected_by: str) -> Request:
    return Request(
        "POST",
        f"/api/agents/{agent_id}/resurrect",
        body={"prompt": prompt, "resurrected_by": resurrected_by},
    )


def register_page_request(agent_id: int, page: PageIntent, idempotency_key: str) -> Request:
    body: dict[str, Any] = {"name": page.name, "port": page.port, "host": page.host}
    if page.title is not None:
        body["title"] = page.title
    if page.serve_dir is not None:
        body["serve_dir"] = page.serve_dir
    if page.ttl_seconds is not None:
        body["ttl_seconds"] = page.ttl_seconds
    return Request(
        "POST",
        f"/api/keyed/v1/agents/{agent_id}/pages",
        body=body,
        idempotency_key=idempotency_key,
    )


def open_page(pages: Sequence[Mapping[str, object]], name: str) -> Mapping[str, object] | None:
    return next((page for page in pages if page["name"] == name), None)


def close_page_request(
    agent_id: int, observed: Mapping[str, object], idempotency_key: str
) -> Request:
    return Request(
        "POST",
        f"/api/keyed/v1/agents/{agent_id}/pages/{observed['name']}/close",
        body={"expected_page_id": observed["id"]},
        idempotency_key=idempotency_key,
    )
