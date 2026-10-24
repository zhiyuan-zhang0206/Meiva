"""Small, single-writer dispatch example with durable receipts and no retry loop.

Call run(handoff, tasks) from an Meiva agent. Each task has an id and a
self-contained prompt including its input. Start with one representative task,
then add peers if useful. The caller validates domain results and chooses when
to collect, wait, or pause. This example does not launch a watcher or enforce
budgets. A changed task needs a new run directory.

Dispatch intent and the spawn receipt cannot be committed atomically. An
interruption between them leaves an ambiguous intent: stop and reconcile it
with actual peers instead of automatically spawning again. Serialize callers.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import meiva


# meiva:note
# The fingerprint that ties a result and a dispatch receipt to the exact prompt that was sent.
def fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# meiva:note
# Refuse a batch that cannot be dispatched, before anything is read or created: it must hold at least one task, every id must be a slug and unique, and every prompt must be nonempty.
def validate_batch(tasks: list[dict[str, str]]) -> None:
    if not tasks:
        raise ValueError("configure at least one task")
    seen = set()
    for task in tasks:
        task_id, prompt = task["id"], task["prompt"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id) or task_id in seen or not prompt.strip():
            raise ValueError("task IDs must be unique slugs and prompts must be nonempty")
        seen.add(task_id)


def _result(text: str | None, path: Path, task_id: str, fingerprint: str) -> dict | None:
    if text is None:
        return None
    value = json.loads(text)
    if (
        not isinstance(value, dict)
        or value.get("task_id") != task_id
        or value.get("input_hash") != fingerprint
        or not isinstance(value.get("result"), dict)
    ):
        raise ValueError(f"invalid or stale result: {path}; reconcile before continuing")
    return value["result"]


class Prepared(NamedTuple):
    task_id: str
    prompt: str
    fingerprint: str
    path: Path


class Plan(NamedTuple):
    results: dict[str, dict]
    pending: dict[str, int]
    prepared: list[Prepared]


# meiva:note
# What one entry of the batch needs, from the receipts in `state` and the result files' texts (`texts`, None for a file that is not there): a validated result is kept; a known peer stays pending and is never redispatched; a task never dispatched is prepared. A changed prompt, an unknown phase, an invalid or stale result, and an intent whose spawn receipt was never recorded all stop the whole batch before any peer is created: the last is an ambiguous dispatch, to reconcile with the actual peers instead of spawning again.
def plan_dispatch(
    handoff: Path,
    tasks: list[dict[str, str]],
    state: dict[str, Any],
    texts: dict[str, str | None],
) -> Plan:
    validate_batch(tasks)
    prepared: list[Prepared] = []
    results: dict[str, dict] = {}
    pending: dict[str, int] = {}
    for task in tasks:
        task_id, prompt = task["id"], task["prompt"]
        digest = fingerprint(prompt)
        record = state.get(task_id)
        if record is not None:
            if record["input_hash"] != digest:
                raise ValueError(f"task {task_id} changed; use a new run directory")
            if record["phase"] not in ("dispatching", "assigned"):
                raise ValueError(f"unknown dispatch phase for {task_id}")
        path = handoff / f"{task_id}.json"
        result = _result(texts[task_id], path, task_id, digest)
        if result is not None:
            results[task_id] = result
        elif record is not None:
            if record["phase"] == "dispatching":
                raise RuntimeError(
                    f"ambiguous dispatch for {task_id}; reconcile actual peers first"
                )
            pending[task_id] = record["agent_id"]
        else:
            prepared.append(Prepared(task_id, prompt, digest, path))
    return Plan(results, pending, prepared)


def _save(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def run(handoff: Path, tasks: list[dict[str, str]]) -> dict:
    """Dispatch new units once; return results and known pending peer IDs.

    Re-entry preserves matching results and never redispatches a known peer.
    Failed or ambiguous spawn attempts propagate before any waiting. Recovery
    and retries are decisions for the calling agent, not automatic defaults.
    """
    validate_batch(tasks)
    handoff.mkdir(parents=True, exist_ok=True)
    state_path = handoff / "dispatch.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    texts = {task["id"]: _read(handoff / f"{task['id']}.json") for task in tasks}
    plan = plan_dispatch(handoff, tasks, state, texts)
    pending = dict(plan.pending)
    for task_id, prompt, digest, path in plan.prepared:
        state[task_id] = {"input_hash": digest, "phase": "dispatching"}
        _save(state_path, state)
        envelope = {"task_id": task_id, "input_hash": digest, "result": {}}
        peer_id = meiva.agents.spawn(
            prompt=(
                f"{prompt}\n\nWrite your result atomically to {path}: write a temporary file "
                "and replace the target when complete. Use this JSON envelope, filling result "
                f"with your domain output: {json.dumps(envelope)}. "
                "The file is the routine handoff; report blockers or budget decisions directly "
                "to the responsible peer instead of making partial output look complete. "
                "Choose whether to idle or end yourself after delivery based on follow-up needs."
            ),
            idempotency_key=str(uuid4()),
        )
        state[task_id] = {"input_hash": digest, "phase": "assigned", "agent_id": int(peer_id)}
        _save(state_path, state)
        pending[task_id] = int(peer_id)
    return {"results": plan.results, "pending": pending}
