# meiva:note
# The lifecycle requests the SDK sends for one agent, as values: the same contract as
# `meiva/gateway_client/plans.py`. The actor a call acts as and the idempotency keys are minted by
# the facade and arrive here as strings.
# meiva:link meiva/agents/docs/agents.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping

from meiva.gateway_client.plans import Request

__all__ = [
    "restart_request",
    "resurrect_request",
    "terminate_request",
]


def terminate_request(
    agent_id: int,
    *,
    source: str,
    message: str | None,
    force: bool,
    kill_all_shell_sessions: bool,
) -> Request:
    body: dict[str, object] = {"source": source}
    if message is not None:
        body["message"] = message
    if force:
        body["force"] = True
    if kill_all_shell_sessions:
        body["kill_all_shell_sessions"] = True
    return Request("POST", f"/api/agents/{agent_id}/terminate", body=body)


def restart_request(
    agent_id: int,
    source: str,
    *,
    message: str,
    config_overlay: Mapping[str, object] | None = None,
) -> Request:
    body: dict[str, object] = {"source": source, "message": message}
    if config_overlay:
        body["config_overlay"] = dict(config_overlay)
    return Request("POST", f"/api/agents/{agent_id}/restart", body=body)


def resurrect_request(agent_id: int, prompt: str, resurrected_by: str) -> Request:
    return Request(
        "POST",
        f"/api/agents/{agent_id}/resurrect",
        body={"prompt": prompt, "resurrected_by": resurrected_by},
    )
