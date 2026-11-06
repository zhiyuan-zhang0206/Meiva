"""Evaluate a skill by re-running its tasks with fresh agents and scoring them.

This is the measurement half of the optimization loop (see the Evaluation Loop
section of SKILL.md). Given a skill and a set of that skill's dataset tasks, it
spawns a fresh agent per task (with `meiva.agents.spawn`, so the agent picks up
the current skill text), lets each run the task, verifies its replay, audits
it for result leaks, then scores the resulting trace with `rubric.py`.
Invalid runs remain in the report but are excluded from the mean. Comparing
the mean score before vs after a skill edit is the signal that tells you
whether the edit helped.

Two-phase, because a task run takes minutes and a single execute_code block is
capped well below that. So you cannot spawn and block for the result in one
turn:

    state = launch(skill, tasks)      # spawns fresh agents, returns immediately
    # ... let them run: watch until poll(state) shows none pending ...
    report = gather(state)            # collect + score each finished agent

`launch` must run inside an agent process (it spawns). `poll` / `gather` only
read the database, so they work from anywhere.

SAFETY: only tasks that are safe to re-run are spawned — spawning replays the
task for real, so a task that sent a message or hit an external API is skipped
(the `is_replay_safe` gate below). Do not evaluate on side-effecting
tasks.

Import it from an agent's code (the skill directory name has a hyphen, so add
the scripts directory to sys.path and `import evaluate`), not as a CLI —
`launch` needs a live agent identity.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict

from psycopg.rows import class_row
from pydantic import TypeAdapter

import meiva
from base.db import Database
from base.host.boundary.untyped import object_of

# `audit`/`collect`/`rubric` are siblings in this skill's own scripts/ dir,
# not an importable package (PYTHONSAFEPATH=1 keeps a script's own directory
# off sys.path) — restore it for the sibling import. Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed
# guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import LeakPaths
from collect import collect_one
from events_window import week_label
from record import task_prompt_of
from record_shape import LeakAuditEntry, Record, RecordBody
from rubric import Scores, scores

from base.cluster.machine import GatewayAccess
from base.paths import meiva_home, memory_dir, workspace_dir

# Tool-call prefixes that make a run unsafe to re-run: fleet/user-facing side
# effects, process lifecycle, arbitrary OS (shell can rm / curl / git push),
# scheduling, and real file writes. A run is replay-safe iff NONE of its
# tools_called match these — leaving pure read/compute tasks.
UNSAFE_PREFIXES = (
    "meiva.agents.",
    "meiva.ui.",
    "meiva.self.",
    "meiva.shell.",
    "meiva.watcher.",
    "meiva.files.edit",
    "meiva.files.write",
    "meiva.files.delete",
    "meiva.mcps.",
)


def is_replay_safe(rec: RecordBody) -> tuple[bool, str]:
    """Return (safe, reason). A run is safe to re-run only if every tool it
    called is pure read/compute and it actually carries a task prompt."""
    if not task_prompt_of(rec).strip():
        return False, "no task prompt"
    hits = sorted({tool for tool in rec["tools_called"] if tool.startswith(UNSAFE_PREFIXES)})
    if hits:
        return False, "side-effect tools: " + ", ".join(hits)
    return True, "pure read/compute"


def verify_replay(original_tools_called: dict[str, int], replayed: Record) -> tuple[bool, str]:
    """Check that a completed replay attempted the original task safely."""
    if replayed["turns"] < 1:
        return False, "replay never ran a turn"
    if not replayed["final_output"].strip():
        return False, "replay produced no output"

    replay_tools = set(replayed["tools_called"])
    unsafe = sorted(tool for tool in replay_tools if tool.startswith(UNSAFE_PREFIXES))
    if unsafe:
        return False, "replay used side-effect tools: " + ", ".join(unsafe)

    original_tools = set(original_tools_called)
    if original_tools and not replay_tools & original_tools:
        return False, (
            "replay tool profile diverges from original "
            f"(original: {', '.join(sorted(original_tools))}, "
            f"replay: {', '.join(sorted(replay_tools))})"
        )
    return True, "ok"


# A spawned eval agent is finished once it stops working: it either idled after
# delivering, or terminated.
DONE_STATUSES = ("idling", "terminated")


# meiva:note
# One replayed task as `launch` recorded it: the eval agent that re-runs it, the task's source
# agent, the prompt it was given and the tool profile the original run had.
class EvalRun(TypedDict):
    eval_agent_id: int
    source_agent_id: int
    prompt: str
    original_tools_called: dict[str, int]
    debrief_sent: NotRequired[bool]


# meiva:note
# One task `launch` declined to replay, and the one reason it is not safe to re-run.
class SkippedTask(TypedDict):
    source_agent_id: int
    reason: str


# meiva:note
# The eval state one `launch` persisted: the skill, the stamp its file is named by, the replays it
# started and the tasks it skipped.
class EvalState(TypedDict):
    skill: str
    stamp: str
    runs: list[EvalRun]
    skipped: list[SkippedTask]
    path: NotRequired[str]


# meiva:note
# Which eval agents are finished and which still run.
class Progress(TypedDict):
    done: list[int]
    pending: list[int]


# meiva:note
# Whether a replay counted towards the mean, and the one reason it did not: a leak, an abstention,
# or the replay check.
class Verification(TypedDict):
    ok: bool
    reason: str


# meiva:note
# One replayed task's report: its scores, its leak audit and whether it entered the mean.
class TaskReport(TypedDict):
    eval_agent_id: int
    source_agent_id: int
    label: str
    scores: Scores
    leak_audit: list[LeakAuditEntry]
    invalidated: bool
    valid: bool
    verification: Verification


# meiva:note
# The mean of the three scored dimensions over the valid replays.
class ScoreMean(TypedDict):
    completion: float
    efficiency: float
    overall: float


# meiva:note
# What `gather` reports, and what `aggregate` reads back from its file: the mean, every replay, the
# invalid ones, the agents still pending and the tasks `launch` skipped.
class EvalReport(TypedDict):
    skill: str
    stamp: str
    n: int
    mean: ScoreMean | None
    per_task: list[TaskReport]
    invalid: list[TaskReport]
    invalidated: int
    pending: list[int]
    skipped: list[SkippedTask]


# meiva:note
# One debrief message sent to a finished eval agent.
class DebriefResult(TypedDict):
    eval_agent_id: int
    prompt: str
    debrief_sent: bool


# meiva:note
# One `agents_meta` lifecycle row: the agent and its current status.
@dataclass(frozen=True)
class _StatusRow:
    id: int
    status: str


_STATE = TypeAdapter(EvalState)


# meiva:note
# One eval state file's contents, checked field by field. A state file this shape does not name a
# field for keeps only the named ones; `poll`/`gather` read nothing else.
def read_eval_state(value: object) -> EvalState:
    return _STATE.validate_python(value, strict=True)


def _eval_dir() -> Path:
    d = meiva_home() / "self_evolution" / "eval"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(skill: str, stamp: str) -> Path:
    safe = skill.replace("/", "_").replace(":", "_")
    return _eval_dir() / f"{safe}-{stamp}.json"


def launch(skill: str, tasks: list[RecordBody], *, model: str | None = None) -> EvalState:
    """Spawn one fresh agent per replay-safe task to re-run it under the current
    skill text. Returns (and persists) the eval state; pass it to `gather` once
    the agents finish. Must run inside an agent process."""
    import meiva

    runs: list[EvalRun] = []
    skipped: list[SkippedTask] = []
    for task in tasks:
        safe, why = is_replay_safe(task)
        if not safe:
            skipped.append(SkippedTask(source_agent_id=task["agent_id"], reason=why))
            continue
        overlay: dict[str, object] = {"eval_isolation": True}
        if model:
            overlay["llm_model"] = model
        prompt = task_prompt_of(task)
        eval_id = meiva.agents.spawn(
            prompt=prompt,
            config_overlay=overlay,
        )
        runs.append(
            EvalRun(
                eval_agent_id=eval_id,
                source_agent_id=task["agent_id"],
                prompt=prompt,
                original_tools_called=task["tools_called"],
            )
        )
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    state = EvalState(skill=skill, stamp=stamp, runs=runs, skipped=skipped)
    path = _state_path(skill, stamp)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    state["path"] = str(path)
    return state


def _status_of(agent_ids: list[int], *, database: Database) -> dict[int, str]:
    if not agent_ids:
        return {}
    with database.connect() as conn, conn.cursor(row_factory=class_row(_StatusRow)) as cur:
        cur.execute("SELECT id, status FROM agents_meta WHERE id = ANY(%s)", [agent_ids])
        return {row.id: row.status for row in cur.fetchall()}


def poll(state: EvalState, *, database: Database) -> Progress:
    """Split the eval agents into done vs still-pending by their current status.
    Call each turn until `pending` is empty, then `gather`, using the same
    caller-supplied Database."""
    ids = [r["eval_agent_id"] for r in state["runs"]]
    return split_progress(ids, _status_of(ids, database=database))


# meiva:note
# Which eval agents are finished and which still run, from the statuses read for them: an agent
# with no status row counts as still pending.
def split_progress(run_ids: list[int], statuses: dict[int, str]) -> Progress:
    done = [i for i in run_ids if statuses.get(i) in DONE_STATUSES]
    pending = [i for i in run_ids if i not in done]
    return Progress(done=done, pending=pending)


def _reason(leak_invalidated: bool, abstain: str | None, replay_reason: str) -> str:
    if leak_invalidated:
        return "leak"
    if abstain is not None:
        return f"abstain:{abstain}"
    return replay_reason


# meiva:note
# The three dimensions of a scored run that all carry a number, or nothing when one of them
# abstained: the mean is only taken over the runs that were scored on every dimension.
def _score_terms(scored: Scores) -> ScoreMean | None:
    completion, efficiency, overall = scored["completion"], scored["efficiency"], scored["overall"]
    if completion is None or efficiency is None or overall is None:
        return None
    return ScoreMean(completion=completion, efficiency=efficiency, overall=overall)


def _mean(score_dicts: list[ScoreMean]) -> ScoreMean | None:
    if not score_dicts:
        return None
    count = len(score_dicts)
    return ScoreMean(
        completion=round(sum(s["completion"] for s in score_dicts) / count, 3),
        efficiency=round(sum(s["efficiency"] for s in score_dicts) / count, 3),
        overall=round(sum(s["overall"] for s in score_dicts) / count, 3),
    )


def _leak_paths(agent_id: int) -> LeakPaths:
    """Build host-side result-separation paths for one evaluation agent."""
    home = meiva_home()
    agent_workspace = workspace_dir(agent_id, home=home).resolve()
    return LeakPaths(
        memory_pool=str(memory_dir(home=home).resolve()),
        self_evolution=str((home / "self_evolution").resolve()),
        workspaces_root=str(agent_workspace.parent),
        agent_workspace=str(agent_workspace),
    )


def gather(state: EvalState, *, database: Database, gateway: GatewayAccess) -> EvalReport:
    """Collect + score every finished eval agent.

    Agents still running are listed under `pending`. Finished replays that do
    not produce output, use side-effect tools, diverge from the original tool
    profile, or touch a leak surface remain in `per_task` and `invalid`, but
    are excluded from the mean and `n`.
    """
    progress = poll(state, database=database)
    now = datetime.now(UTC)
    records: dict[int, Record] = {
        run["eval_agent_id"]: collect_one(
            run["eval_agent_id"],
            week_label(now),
            _leak_paths(run["eval_agent_id"]),
            database=database,
            gateway=gateway,
            now=now,
        )
        for run in state["runs"]
        if run["eval_agent_id"] in progress["done"]
    }
    return assemble_report(state, progress["pending"], records)


def _evaluate_task(run: EvalRun, record: Record) -> TaskReport:
    rec = Record(**record)
    if not rec["task_prompt"].strip():
        rec["task_prompt"] = task_prompt_of(rec)
    replay_ok, replay_reason = verify_replay(run["original_tools_called"], rec)
    leak_invalidated = rec.get("invalidated")
    if leak_invalidated is None:
        raise TypeError(f"eval record for agent {run['eval_agent_id']} carries no leak audit")
    scored = scores(rec)
    abstain = scored.get("abstain")
    valid = replay_ok and not leak_invalidated and abstain is None
    reason = _reason(leak_invalidated, abstain, replay_reason)
    return TaskReport(
        eval_agent_id=run["eval_agent_id"],
        source_agent_id=run["source_agent_id"],
        label=rec["label"],
        scores=scored,
        leak_audit=rec.get("leak_audit", []),
        invalidated=leak_invalidated,
        valid=valid,
        verification=Verification(ok=valid, reason=reason),
    )


# meiva:note
# The report over the records collected for the finished eval agents (`records`, by eval agent
# id; the runs without a record are not reported on). Each replay is verified against its
# original tool profile and scored; one that never ran, used side-effect tools, diverged, abstained
# or touched a leak surface stays in `per_task` and `invalid` and is left out of the mean and `n`.
# The records are not changed.
def assemble_report(state: EvalState, pending: list[int], records: dict[int, Record]) -> EvalReport:
    per_task = [
        _evaluate_task(run, records[run["eval_agent_id"]])
        for run in state["runs"]
        if run["eval_agent_id"] in records
    ]
    invalid = [task for task in per_task if not task["valid"]]
    valid = [task for task in per_task if task["valid"]]
    return EvalReport(
        skill=state["skill"],
        stamp=state["stamp"],
        n=len(valid),
        mean=_mean(
            [terms for task in valid if (terms := _score_terms(task["scores"])) is not None]
        ),
        per_task=per_task,
        invalid=invalid,
        invalidated=sum(task["invalidated"] for task in per_task),
        pending=pending,
        skipped=state["skipped"],
    )


def latest_state(skill: str) -> EvalState:
    """Reload the most recent eval state for a skill (state does not survive
    across turns in memory, so `gather` reloads it from disk)."""
    safe = skill.replace("/", "_").replace(":", "_")
    files = sorted(_eval_dir().glob(f"{safe}-*.json"))
    if not files:
        raise FileNotFoundError(f"no eval state for skill {skill!r} under {_eval_dir()}")
    return read_eval_state(object_of(json.loads, files[-1].read_text(encoding="utf-8")))


# ─────────────── debrief (the backward pass) ───────────────


def debrief(state: EvalState, skill_name: str, *, database: Database) -> list[DebriefResult]:
    """Ask each eval agent that just ran a task: "what would you change?"

    This is the backward pass of the evaluation loop — the agent that actually
    did the work has first-hand experience of what the skill got wrong.  Its
    self-reflection is 80% of the gradient signal; trace-mining by a separate
    worker is the other 20%.

    Returns one record per debriefed agent: {eval_agent_id, prompt, debrief_sent}.
    """
    results: list[DebriefResult] = []
    status = _status_of([entry["eval_agent_id"] for entry in state["runs"]], database=database)
    for entry in state["runs"]:
        eval_agent_id = entry["eval_agent_id"]
        if status.get(eval_agent_id) not in DONE_STATUSES:
            continue
        try:
            meiva.agents.send_message(
                eval_agent_id,
                f"You just completed a task using the skill '{skill_name}'. "
                "Based on your experience running this task, what specific "
                "changes to the skill instructions would have helped you "
                "complete it better, faster, or with fewer errors? "
                "Be concrete: point to steps, prompt wording, missing context, "
                "or ambiguous instructions. Reply in 3-5 lines.",
            )
        except Exception as exc:
            print(f"debrief to eval agent {eval_agent_id} failed: {exc!r}", file=sys.stderr)
            continue
        entry["debrief_sent"] = True
        results.append(
            DebriefResult(
                eval_agent_id=eval_agent_id,
                prompt=entry["prompt"],
                debrief_sent=True,
            )
        )
    return results
