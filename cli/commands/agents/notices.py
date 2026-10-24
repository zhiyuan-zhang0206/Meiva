# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md

from __future__ import annotations

from typing import Any, TextIO
from uuid import uuid4

from base.api_contracts.idempotency import keyed_headers
from base.api_contracts.notice_question import parse_question, question_instructions
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net import http_dial

_TIMEOUT_S = 15.0


# meiva:note
# Auth headers plus one fresh principal-v1 operation key for a resolve call.
def _keyed_headers(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, **keyed_headers(str(uuid4()))}


def _api(access: GatewayAccess) -> str:
    return f"{gateway_api_base(access.gateway_url)}/api"


def _print_notice(
    n: dict[str, Any], i: int, total: int, *, agent_status: str | None = None
) -> None:
    kind = "NOTICE"
    label = n.get("agent_label") or ""
    status = f" [{agent_status}]" if agent_status else ""
    print(
        f"[{i}/{total}] #{n['id']} {kind} {n['priority']} "
        f"agent#{n['agent_id']} {label}{status}: {n['title']}"
    )
    if n.get("content"):
        print(f"    {n['content'][:120]}")
    if n.get("question") is not None:
        print(question_instructions(parse_question(n["question"])))


# meiva:note
# Resolve only agents referenced by the bounded notice result.
#
# Missing agents remain unknown and cannot authorize stale-notice cleanup.
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
        statuses[agent_id] = resp.json()["status"]
    return statuses


def _fetch_open_notices(access: GatewayAccess) -> list[dict[str, Any]]:
    resp = http_dial.get(
        f"{_api(access)}/notices/open",
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(access),
        params={"include_awaiting": True},
    )
    resp.raise_for_status()
    return resp.json()


def _filter_notices(
    notices: list[dict[str, Any]],
    *,
    agent_id: int | None,
    priority: str | None,
    type_filter: str | None,
) -> list[dict[str, Any]]:
    if agent_id is not None:
        notices = [n for n in notices if n["agent_id"] == agent_id]
    if priority:
        notices = [n for n in notices if n["priority"] == priority.upper()]
    if type_filter == "fyi":
        notices = [n for n in notices if not n["require_response"]]
    elif type_filter == "decision":
        notices = [n for n in notices if n["require_response"]]
    return notices


# meiva:note
# List what `clear` will resolve and ask for confirmation. An empty or closed stdin reads as
# no.
def _confirm_clear(
    notices: list[dict[str, Any]], *, stale: bool, agent_id: int | None, stdin: TextIO
) -> bool:
    for i, n in enumerate(notices, start=1):
        _print_notice(n, i, len(notices))
    target = "stale (terminated-agent) notices" if stale else f"agent #{agent_id}"
    print(
        f"Clear {len(notices)} notice(s): {target}? "
        "(dismiss without answering; re-run with --force to skip this prompt)"
    )
    print("y/N: ", end="", flush=True)
    answer = stdin.readline().strip().lower()
    return answer in ("y", "yes")


# meiva:note
# `meiva notices list` — every open notice across the fleet (or one agent).
#
# --stale narrows to notices whose agent is terminated: the invalid backlog of dead agents,
# as opposed to live agents' open notices (Task #1149).
def cmd_notices_list(
    access: GatewayAccess,
    *,
    agent_id: int | None,
    priority: str | None,
    type_filter: str | None,
    stale: bool = False,
) -> int:
    notices = _fetch_open_notices(access)
    notices = _filter_notices(
        notices, agent_id=agent_id, priority=priority, type_filter=type_filter
    )
    statuses: dict[int, str] = {}
    if stale:
        statuses = _agent_status_map(access, {n["agent_id"] for n in notices})
        notices = [n for n in notices if statuses.get(n["agent_id"]) == "terminated"]
    if not notices:
        print("No open notices." if not stale else "No stale notices (terminated agents).")
        return 0
    total = len(notices)
    for i, n in enumerate(notices, start=1):
        _print_notice(n, i, total, agent_status=statuses.get(n["agent_id"]) if stale else None)
    return 0


# meiva:note
# `meiva notices resolve <id> --agent <id> --action ...` — resolve one notice.
def cmd_notices_resolve(
    access: GatewayAccess, *, notice_id: int, agent_id: int, action: str, reply: str | None
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
        headers=_keyed_headers(gateway_auth_headers(access)),
        json={"action": action, "reply": reply},
    )
    resp.raise_for_status()
    print(f"notice #{notice_id} resolved: {action}")
    return 0


# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md
def _resolve_all(access: GatewayAccess, notices: list[dict[str, Any]]) -> int:
    cleared = 0
    for n in notices:
        r = http_dial.post(
            f"{_api(access)}/keyed/v1/agents/{n['agent_id']}/notices/{n['id']}/resolve",
            timeout=_TIMEOUT_S,
            headers=_keyed_headers(gateway_auth_headers(access)),
            json={"action": "dismiss", "reply": None},
        )
        if r.status_code in (200, 201):
            cleared += 1
    return cleared


# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md
def cmd_notices_clear(
    access: GatewayAccess, *, agent_id: int | None, force: bool, stale: bool = False, stdin: TextIO
) -> int:
    if stale and agent_id is not None:
        print("--stale and --agent are mutually exclusive")
        return 2
    notices = _fetch_open_notices(access)
    if stale:
        statuses = _agent_status_map(access, {n["agent_id"] for n in notices})
        notices = [n for n in notices if statuses.get(n["agent_id"]) == "terminated"]
    else:
        notices = [n for n in notices if n["agent_id"] == agent_id]
    if not notices:
        print(
            "No stale notices (terminated agents)."
            if stale
            else f"agent #{agent_id}: no open notices."
        )
        return 0
    if not force and not _confirm_clear(notices, stale=stale, agent_id=agent_id, stdin=stdin):
        print("aborted.")
        return 1
    cleared = _resolve_all(access, notices)
    target = "stale notices" if stale else f"agent #{agent_id}"
    print(f"cleared {cleared}/{len(notices)} notices: {target}")
    return 0
