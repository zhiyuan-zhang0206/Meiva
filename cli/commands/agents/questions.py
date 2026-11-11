# meiva:link cli/docs/operator-surfaces/operator-surfaces.md

from __future__ import annotations

import random
from typing import TextIO

from pydantic import BaseModel, JsonValue, TypeAdapter

from base.api_contracts.idempotency import keyed_headers
from base.api_contracts.question import parse_question, question_instructions
from base.clock.ids import new_uuid
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net import http_dial

_TIMEOUT_S = 15.0


# meiva:note
# Auth headers plus the principal-v1 operation key of one submission.
def _keyed_headers(headers: dict[str, str], idempotency_key: str) -> dict[str, str]:
    return {**headers, **keyed_headers(idempotency_key)}


def _api(access: GatewayAccess) -> str:
    return f"{gateway_api_base(access.gateway_url)}/api"


# meiva:note
# One unresolved question as the gateway lists it, with the fields the CLI reads. Other fields of the
# answer are ignored.
class _Question(BaseModel):
    id: int
    agent_id: int
    title: str
    agent_label: str | None = None
    source_verified_by: str | None = None
    content: str | None = None
    question: JsonValue = None


_QUESTIONS = TypeAdapter(list[_Question])


class _Conflict(BaseModel):
    open: list[_Question]


class _AgentStatus(BaseModel):
    status: str


def _print_question(n: _Question, i: int, total: int, *, agent_status: str | None = None) -> None:
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


def _fetch_open_questions(access: GatewayAccess, *, agent_id: int | None = None) -> list[_Question]:
    resp = http_dial.get(
        f"{_api(access)}/questions/open",
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(access),
        params=None if agent_id is None else {"agent_id": agent_id},
    )
    resp.raise_for_status()
    return _QUESTIONS.validate_json(resp.content)


def _filter_questions(questions: list[_Question], *, agent_id: int | None) -> list[_Question]:
    if agent_id is None:
        return questions
    return [n for n in questions if n.agent_id == agent_id]


# meiva:note
# List what `skip` will skip and ask for confirmation. An empty or closed stdin reads as
# no.
def _confirm_skip(
    questions: list[_Question], *, stale: bool, agent_id: int | None, stdin: TextIO
) -> bool:
    for i, n in enumerate(questions, start=1):
        _print_question(n, i, len(questions))
    target = "stale (terminated-agent) questions" if stale else f"agent #{agent_id}"
    print(
        f"Skip {len(questions)} question(s): {target}? "
        "(skipped without an answer; re-run with --force to skip this prompt)"
    )
    print("y/N: ", end="", flush=True)
    answer = stdin.readline().strip().lower()
    return answer in ("y", "yes")


# meiva:note
# `meiva questions list` — every unresolved question across the fleet (or one agent).
#
# --stale narrows to questions whose agent is terminated: the invalid backlog of dead agents,
# as opposed to live agents' open questions (Task #1149).
def cmd_questions_list(
    access: GatewayAccess,
    *,
    agent_id: int | None,
    stale: bool = False,
) -> int:
    questions = _fetch_open_questions(access)
    questions = _filter_questions(questions, agent_id=agent_id)
    statuses: dict[int, str] = {}
    if stale:
        statuses = _agent_status_map(access, {n.agent_id for n in questions})
        questions = [n for n in questions if statuses.get(n.agent_id) == "terminated"]
    if not questions:
        print("No open questions." if not stale else "No stale questions (terminated agents).")
        return 0
    total = len(questions)
    for i, n in enumerate(questions, start=1):
        _print_question(n, i, total, agent_status=statuses.get(n.agent_id) if stale else None)
    return 0


# meiva:note
# The gateway settles all of an agent's open questions in one submission, so the CLI reads the
# agent's open set, requires every one to be answered or skipped, and posts the submission it just
# read. A changed set is the gateway's 409: nothing is settled and the latest ids are printed.
def _submit(
    access: GatewayAccess,
    agent_id: int,
    results: list[dict[str, JsonValue]],
    expected_ids: list[int],
    idempotency_key: str,
) -> int:
    resp = http_dial.post(
        f"{_api(access)}/keyed/v1/agents/{agent_id}/questions/answers",
        timeout=_TIMEOUT_S,
        headers=_keyed_headers(gateway_auth_headers(access), idempotency_key),
        json={"expected_ids": expected_ids, "results": results},
    )
    if resp.status_code == 409:
        latest = [item.id for item in _Conflict.model_validate_json(resp.content).open]
        print(f"agent #{agent_id}: the open questions changed, nothing settled; open now: {latest}")
        return 1
    resp.raise_for_status()
    return 0


# meiva:note
# `meiva questions answer --agent N [--answer ID=TEXT]... [--skip ID]...` — settle every open
# question of one agent. TEXT is a numbered reply (option numbers on the first line, then an
# optional comment; plain text for a text question).
def cmd_questions_answer(
    access: GatewayAccess,
    *,
    agent_id: int,
    answers: list[tuple[int, str]],
    skips: list[int],
    idempotency_key: str,
) -> int:
    open_ids = [n.id for n in _fetch_open_questions(access, agent_id=agent_id)]
    if not open_ids:
        print(f"agent #{agent_id}: no open questions.")
        return 0
    given = [question_id for question_id, _ in answers] + skips
    if sorted(given) != sorted(open_ids):
        print(
            f"agent #{agent_id}: every open question must be answered or skipped once; "
            f"open: {open_ids}, given: {sorted(given)}"
        )
        return 2
    results: list[dict[str, JsonValue]] = []
    for question_id, reply in answers:
        results.append({"id": question_id, "reply": reply})
    for question_id in skips:
        results.append({"id": question_id, "skip": True})
    code = _submit(access, agent_id, results, open_ids, idempotency_key)
    if code == 0:
        print(f"agent #{agent_id}: settled {len(open_ids)} question(s)")
    return code


# meiva:note
# The open questions a skip acts on: those of one agent, or those of every terminated agent.
def _skip_targets(access: GatewayAccess, *, agent_id: int | None, stale: bool) -> list[_Question]:
    questions = _fetch_open_questions(access)
    if not stale:
        return [n for n in questions if n.agent_id == agent_id]
    statuses = _agent_status_map(access, {n.agent_id for n in questions})
    return [n for n in questions if statuses.get(n.agent_id) == "terminated"]


# meiva:note
# `meiva questions skip --agent N` or `--stale` — skip every open question of one agent, or of every
# terminated agent, after confirmation.
def cmd_questions_skip(
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
    questions = _skip_targets(access, agent_id=agent_id, stale=stale)
    if not questions:
        print(
            "No stale questions (terminated agents)."
            if stale
            else f"agent #{agent_id}: no open questions."
        )
        return 0
    if not force and not _confirm_skip(questions, stale=stale, agent_id=agent_id, stdin=stdin):
        print("aborted.")
        return 1
    by_agent: dict[int, list[int]] = {}
    for n in questions:
        by_agent.setdefault(n.agent_id, []).append(n.id)
    skipped = 0
    for owner, ids in by_agent.items():
        results: list[dict[str, JsonValue]] = [{"id": i, "skip": True} for i in ids]
        if _submit(access, owner, results, ids, new_uuid(rng).hex) == 0:
            skipped += len(ids)
    target = "stale questions" if stale else f"agent #{agent_id}"
    print(f"skipped {skipped}/{len(questions)} questions: {target}")
    return 0
