# meiva:link cli/docs/operator-surfaces/operator-surfaces.md

from __future__ import annotations

import random
from typing import TextIO

from pydantic import BaseModel, JsonValue, TypeAdapter

from base.api_contracts.idempotency import keyed_headers
from base.api_contracts.notice_question import parse_question, question_instructions
from base.clock.ids import new_uuid
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net import http_dial

_TIMEOUT_S = 15.0


# meiva:note
# Auth headers plus the principal-v1 operation key of one resolve call.
def _keyed_headers(headers: dict[str, str], idempotency_key: str) -> dict[str, str]:
    return {**headers, **keyed_headers(idempotency_key)}


def _api(access: GatewayAccess) -> str:
    return f"{gateway_api_base(access.gateway_url)}/api"


# meiva:note
# One unresolved question as the gateway lists it, with the fields the CLI reads. Other fields of the
# answer are ignored.
class _Notice(BaseModel):
    id: int
    agent_id: int
    title: str
    agent_label: str | None = None
    source_verified_by: str | None = None
    content: str | None = None
    question: JsonValue = None


_NOTICES = TypeAdapter(list[_Notice])


class _AgentStatus(BaseModel):
    status: str


def _print_notice(n: _Notice, i: int, total: int, *, agent_status: str | None = None) -> None:
    kind = "QUESTION"
    label = n.agent_label or ""
    status = f" [{agent_status}]" if agent_status else ""
    # meiva:note
    # `source` is the credential the SERVER verified for the write, not the caller's
    # claim; it is what answers "which credential posted this", which the URL's
    # agent_id cannot. Absent on rows written before the column existed.
    source = n.source_verified_by
    print(
        f"[{i}/{total}] #{n.id} {kind} "
        f"agent#{n.agent_id} {label}{status}: {n.title}"
        + (f"  [source: {source}]" if source else "")
    )
    if n.content:
        print(f"    {n.content[:120]}")
    if n.question is not None:
        print(question_instructions(parse_question(n.question)))


# meiva:note
# Resolve only agents referenced by the bounded question result.
#
# Missing agents remain unknown and cannot authorize stale-question cleanup.
def _agent_status_map(access: GatewayAccess, agent_ids: set[int]) -> dict[int, str]:
    statuses: dict[int, str] = {}
    for agent_id in sorted(agent_ids):
        resp = http_dial.get(
            f"{_api(access)}/agents/{agent_id}",
            timeout=_TIMEOUT_S,
            headers=gateway_auth_headers(access),
        )
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        statuses[agent_id] = _AgentStatus.model_validate_json(resp.content).status
    return statuses


def _fetch_open_notices(access: GatewayAccess) -> list[_Notice]:
    resp = http_dial.get(
        f"{_api(access)}/notices/open",
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(access),
    )
    resp.raise_for_status()
    return _NOTICES.validate_json(resp.content)


def _filter_notices(notices: list[_Notice], *, agent_id: int | None) -> list[_Notice]:
    if agent_id is None:
        return notices
    return [n for n in notices if n.agent_id == agent_id]


# meiva:note
# List what `clear` will resolve and ask for confirmation. An empty or closed stdin reads as
# no.
def _confirm_clear(
    notices: list[_Notice], *, stale: bool, agent_id: int | None, stdin: TextIO
) -> bool:
    for i, n in enumerate(notices, start=1):
        _print_notice(n, i, len(notices))
    target = "stale (terminated-agent) questions" if stale else f"agent #{agent_id}"
    print(
        f"Clear {len(notices)} question(s): {target}? "
        "(dismiss without answering; re-run with --force to skip this prompt)"
    )
    print("y/N: ", end="", flush=True)
    answer = stdin.readline().strip().lower()
    return answer in ("y", "yes")


# meiva:note
# `meiva notices list` — every unresolved question across the fleet (or one agent).
#
# --stale narrows to questions whose agent is terminated: the invalid backlog of dead agents,
# as opposed to live agents' open questions (Task #1149).
def cmd_notices_list(
    access: GatewayAccess,
    *,
    agent_id: int | None,
    stale: bool = False,
) -> int:
    notices = _fetch_open_notices(access)
    notices = _filter_notices(notices, agent_id=agent_id)
    statuses: dict[int, str] = {}
    if stale:
        statuses = _agent_status_map(access, {n.agent_id for n in notices})
        notices = [n for n in notices if statuses.get(n.agent_id) == "terminated"]
    if not notices:
        print("No open questions." if not stale else "No stale questions (terminated agents).")
        return 0
    total = len(notices)
    for i, n in enumerate(notices, start=1):
        _print_notice(n, i, total, agent_status=statuses.get(n.agent_id) if stale else None)
    return 0


# meiva:note
# `meiva notices resolve <id> --agent <id> --action ...` — resolve one question.
def cmd_notices_resolve(
    access: GatewayAccess,
    *,
    notice_id: int,
    agent_id: int,
    action: str,
    reply: str | None,
    idempotency_key: str,
) -> int:
    if action not in ("answer", "dismiss"):
        print(f"invalid action {action!r} (answer|dismiss)")
        return 2
    if action == "answer" and not reply:
        print("--reply TEXT is required for action=answer")
        return 2
    resp = http_dial.post(
        f"{_api(access)}/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve",
        timeout=_TIMEOUT_S,
        headers=_keyed_headers(gateway_auth_headers(access), idempotency_key),
        json={"action": action, "reply": reply},
    )
    resp.raise_for_status()
    print(f"question #{notice_id} resolved: {action}")
    return 0


# meiva:link cli/docs/operator-surfaces/operator-surfaces.md
def _resolve_all(access: GatewayAccess, notices: list[_Notice], rng: random.Random) -> int:
    cleared = 0
    for n in notices:
        r = http_dial.post(
            f"{_api(access)}/keyed/v1/agents/{n.agent_id}/notices/{n.id}/resolve",
            timeout=_TIMEOUT_S,
            headers=_keyed_headers(gateway_auth_headers(access), new_uuid(rng).hex),
            json={"action": "dismiss", "reply": None},
        )
        if r.status_code in (200, 201):
            cleared += 1
    return cleared


# meiva:link cli/docs/operator-surfaces/operator-surfaces.md
def cmd_notices_clear(
    access: GatewayAccess,
    *,
    agent_id: int | None,
    force: bool,
    stale: bool = False,
    stdin: TextIO,
    rng: random.Random,
) -> int:
    if stale and agent_id is not None:
        print("--stale and --agent are mutually exclusive")
        return 2
    notices = _fetch_open_notices(access)
    if stale:
        statuses = _agent_status_map(access, {n.agent_id for n in notices})
        notices = [n for n in notices if statuses.get(n.agent_id) == "terminated"]
    else:
        notices = [n for n in notices if n.agent_id == agent_id]
    if not notices:
        print(
            "No stale questions (terminated agents)."
            if stale
            else f"agent #{agent_id}: no open questions."
        )
        return 0
    if not force and not _confirm_clear(notices, stale=stale, agent_id=agent_id, stdin=stdin):
        print("aborted.")
        return 1
    cleared = _resolve_all(access, notices, rng)
    target = "stale questions" if stale else f"agent #{agent_id}"
    print(f"cleared {cleared}/{len(notices)} questions: {target}")
    return 0
