# meiva:note
# `record.py` shapes one self-evolution dataset record from telemetry events, log events, inbound messages, and checkpoint transcripts while staying separate from `collect.py`'s database and HTTP transport. `count_tool_calls` parses executed code with `SyntaxWarning` suppressed so agent regex escapes do not leak into schedule stderr. `_is_exec_fail` matches only terminal failure event names (`exec_envelope` is a wrapper, not a failure). `_msg_text` extracts human-visible text from checkpoint message blocks and skips `thinking` and `tool_use` blocks so silent tool turns do not serialize as `"thinking tool_use"`. Broadcast inbound instructions are excluded from per-agent `corrections` and `followup_prompts`.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/meiva-self-evolution.meiva.okf.md
# meiva:note
# `record._evidence_block` reports what a record is based on; `record._grade` grades that block in priority order — no telemetry events, or no tokens: `none` / `no_telemetry`; no task prompt: `partial` / `no_task`; a lifecycle status other than `terminated`: `partial` / `no_terminal`; all three present: `complete` with no reason. `prompt_source` names the `_task_prompt` branch that supplied the prompt; `task_updates.note` counts note-only receipts; `task_state_changes` counts status receipts whose status differs from the actor's previous declaration on that task, `in_progress` being the birth state. `collect._task_receipts_by_agent` supplies the receipt counts in one grouped query per agent batch, and the inbounds `collect` already fetches carry `claimed`, so the block adds no per-agent query.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/evidence-block-and-grading.meiva.okf.md
from __future__ import annotations

import ast
import contextlib
import sys
import warnings
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, cast

from langchain_core.messages import BaseMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import LeakPaths, invalidated, scan
from label import label
from record_inputs import AgentMeta as AgentMeta
from record_inputs import EventFact, InboundFact, RunInputs, TaskReceipts, TranscriptRow
from record_inputs import event_facts as event_facts
from record_inputs import inbound_facts as inbound_facts
from record_inputs import transcript_facts as transcript_facts

from base.lm.system_note import SYSTEM_NOTE_OPEN

CN_CORRECTION_KEYWORDS = (
    "\u4e0d\u5bf9",
    "\u9519\u4e86",
    "\u91cd\u65b0",
    "\u4e0d\u8981",
    "\u6539\u6210",
    "\u5e94\u8be5\u662f",
    "\u4e0d\u662f\u8fd9\u6837",
    "\u4f60\u641e\u9519\u4e86",
    "\u641e\u9519\u4e86",
    "\u7ea0\u6b63",
    "\u4fee\u6b63\u4e00\u4e0b",
    "\u4e0d\u662f\u8fd9\u4e2a",
    "\u522b\u8fd9\u6837",
    "\u4e0d\u662f\u8ba9\u4f60",
    "\u4f60\u6ca1\u7406\u89e3",
    "\u7406\u89e3\u9519\u4e86",
    "\u9519\u8bef",
    "\u6709\u8bef",
    "\u8bef\u5bfc",
)

EN_CORRECTION_KEYWORDS = (
    "wrong",
    "incorrect",
    "don't",
    "do not",
    "should be",
    "instead",
    "actually",
    "no,",
    "fix this",
    "correct this",
    "not right",
    "mistake",
    "you misunderstood",
    "that's not",
    "redo",
    "re-do",
    "try again",
)

PEER_FEEDBACK_KEYWORDS = (
    "\u4f60\u90a3\u4e2a",
    "\u4f60\u6ca1",
    "\u4e0d\u8981\u7528",
    "\u522b\u7528",
    "\u6539\u6210",
    "\u4fee\u4e00\u4e0b",
    "\u8fd8\u6ca1\u4fee",
    "\u6ca1\u4fee\u597d",
    "\u4e0d\u5bf9",
    "\u9519\u4e86",
    "\u5e94\u8be5\u7528",
    "\u4e0d\u80fd\u8fd9\u6837",
    "\u6ce8\u610f",
    "\u522b\u5fd8\u4e86",
    "\u63d0\u9192",
    "\u7ea0\u6b63",
)


HUMAN_SOURCES = frozenset({"user", "ui:web"})


def _detect_correction(content: str) -> bool:
    if not content:
        return False
    if any(keyword in content for keyword in CN_CORRECTION_KEYWORDS):
        return True
    content_lower = content.lower()
    return any(keyword in content_lower for keyword in EN_CORRECTION_KEYWORDS)


def _detect_peer_feedback(content: str) -> bool:
    if not content:
        return False
    if any(keyword in content for keyword in PEER_FEEDBACK_KEYWORDS):
        return True
    return _detect_correction(content)


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def count_tool_calls(codes: list[str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for code in codes:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name and name.split(".", 1)[0] == "meiva":
                    counts[name] += 1
    return dict(counts)


def _is_exec_ok(event: str) -> bool:
    return event == "exec"


_EXEC_FAIL_EVENTS = frozenset(
    {
        "exec_failed",
        "exec_timeout",
        "exec_node_timeout",
        "exec_cancelled",
        "exec_subprocess_aborted",
        "exec_subprocess_killed",
        "exec_result_envelope_invalid",
    }
)


def _is_exec_fail(event: str) -> bool:
    return event in _EXEC_FAIL_EVENTS


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in cast(list[Any], content):
            if isinstance(block, dict):
                b_dict = cast(dict[str, Any], block)
                if b_dict.get("type") in {"thinking", "tool_use"}:
                    continue
                parts.append(b_dict.get("text") or b_dict.get("type") or "")
            else:
                parts.append(str(block))
        return " ".join(part for part in parts if part)
    return str(content)


# meiva:note
# The transcript rows of a checkpoint's messages, in conversation order: the message type and its human-visible text.
# `collect.py` reads the checkpoint and passes these rows in, so the record is built from values alone.
def transcript_rows(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    return [{"type": message.type, "content": _msg_text(message.content)} for message in messages]


def _plugins_activated(events: Sequence[EventFact]) -> tuple[dict[str, int], int]:
    fired: Counter[str] = Counter()
    skipped = 0
    for event in events:
        if event.activation_skipped:
            skipped += 1
        elif event.activation is not None:
            fired[event.activation] += 1
    return dict(sorted(fired.items())), skipped


def _mode(values: list[str]) -> str:
    return Counter(value for value in values if value).most_common(1)[0][0] if any(values) else ""


# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
def _task_source(source: str, spawner: str) -> str:
    if source in HUMAN_SOURCES:
        return "human"
    if spawner and source == spawner:
        return "spawner"
    if source.startswith("schedule:"):
        return "schedule"
    return ""


# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
def _first_human_message(transcript: Sequence[TranscriptRow]) -> str:
    for message in transcript:
        content = message.content or ""
        if message.type != "human" or not content.strip():
            continue
        if content.lstrip().startswith(SYSTEM_NOTE_OPEN):
            continue
        return content
    return ""


# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
def _task_prompt(
    spawner: str, messages: Sequence[InboundFact], transcript: Sequence[TranscriptRow]
) -> tuple[str, str]:
    for wanted in ("human", "spawner", "schedule"):
        for message in messages:
            if _task_source(message.source, spawner) != wanted:
                continue
            if message.content.strip():
                return message.content, wanted
    inherited = _first_human_message(transcript)
    return (inherited, "transcript") if inherited else ("", "none")


# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
def task_prompt_of(record: Mapping[str, object]) -> str:
    raw = cast(dict[str, Any], record)
    prompt = (raw.get("task_prompt") or "").strip()
    if prompt:
        return raw["task_prompt"]
    rows = cast(Sequence[Mapping[str, str]], raw.get("transcript") or ())
    return _first_human_message(transcript_facts(rows))


def _peer_feedback(spawner: str, agent_messages: Sequence[InboundFact]) -> list[dict[str, Any]]:
    spawner_id: int | None = None
    if spawner.startswith("agent:"):
        with contextlib.suppress(IndexError, ValueError):
            spawner_id = int(spawner.split(":", 1)[1])
    peer_feedback: list[dict[str, Any]] = []
    for message in agent_messages:
        try:
            from_agent = int(message.source.split(":", 1)[1])
        except (IndexError, ValueError):
            from_agent = 0
        if from_agent == spawner_id:
            continue
        if _detect_peer_feedback(message.content):
            peer_feedback.append({"from_agent": from_agent, "content": message.content})
    return peer_feedback


class _EventStats(NamedTuple):
    turns: int
    models: list[str]
    tokens_in: int
    tokens_out: int
    code_bodies: list[str]
    exec_ok: int
    exec_failed: int
    last_exec_failed: bool
    duration_s: float


def _event_stats(events: Sequence[EventFact]) -> _EventStats:
    turns = exec_ok = exec_failed = tokens_in = tokens_out = 0
    models: list[str] = []
    code_bodies: list[str] = []
    exec_outcomes: list[str] = []
    duration = 0.0
    for fact in events:
        event = fact.name
        if event == "turn_end":
            turns += 1
            duration += fact.duration_s
        elif event == "llm_usage":
            models.append(fact.model)
            tokens_in += fact.tokens_in
            tokens_out += fact.tokens_out
        elif event == "code":
            code_bodies.append(fact.body)
        if _is_exec_ok(event):
            exec_ok += 1
            exec_outcomes.append(event)
        elif _is_exec_fail(event):
            exec_failed += 1
            exec_outcomes.append(event)
    return _EventStats(
        turns=turns,
        models=models,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        code_bodies=code_bodies,
        exec_ok=exec_ok,
        exec_failed=exec_failed,
        last_exec_failed=bool(exec_outcomes) and _is_exec_fail(exec_outcomes[-1]),
        duration_s=round(duration, 1),
    )


def _leak_fields(code_bodies: list[str], agent_id: int, leak_paths: LeakPaths) -> dict[str, Any]:
    findings = scan(code_bodies, agent_id=agent_id, leak_paths=leak_paths)
    return {
        "leak_audit": [
            {"surface": finding.surface, "evidence": finding.evidence, "tool": finding.tool}
            for finding in findings
        ],
        "invalidated": invalidated(findings),
    }


def _prompt_fields(
    spawner: str, inbounds: Sequence[InboundFact], transcript: Sequence[TranscriptRow]
) -> dict[str, Any]:
    per_agent = [message for message in inbounds if not message.is_broadcast]
    human_messages = [message.content for message in per_agent if message.source in HUMAN_SOURCES]
    agent_messages = [message for message in per_agent if message.source.startswith("agent:")]
    task_prompt, task_prompt_source = _task_prompt(spawner, per_agent, transcript)
    followups = human_messages[1:] if task_prompt_source == "human" else human_messages
    return {
        "task_prompt": task_prompt,
        "task_prompt_source": task_prompt_source,
        "followup_prompts": [m for m in followups if not _detect_correction(m)],
        "corrections": [m for m in followups if _detect_correction(m)],
        "peer_feedback": _peer_feedback(spawner, agent_messages),
    }


def _grade(ev: dict[str, Any]) -> tuple[str, str | None]:
    if ev["telemetry_events"] == 0:
        return "none", "no_telemetry"
    if ev["tokens_in"] + ev["tokens_out"] == 0:
        return "none", "no_telemetry"
    if ev["task_prompt_chars"] == 0:
        return "partial", "no_task"
    if ev["terminal_status"] != "terminated":
        return "partial", "no_terminal"
    return "complete", None


def _evidence_block(
    run: RunInputs, stats: _EventStats, prompts: dict[str, Any], final_output: str
) -> dict[str, Any]:
    receipts = run.task_receipts if run.task_receipts is not None else TaskReceipts(0, 0, 0, 0)
    return {
        "telemetry_events": len(run.events),
        "audit_events": len(run.log_events),
        "tokens_in": stats.tokens_in,
        "tokens_out": stats.tokens_out,
        "turns": stats.turns,
        "task_prompt_chars": len(prompts["task_prompt"].strip()),
        "prompt_source": prompts["task_prompt_source"],
        "final_output_chars": len(final_output.strip()),
        "terminal_status": run.meta.status,
        "inbound_total": len(run.inbounds),
        "inbound_claimed": sum(1 for message in run.inbounds if message.claimed),
        "task_updates": {
            "note": receipts.note,
            "status": receipts.status,
            "results": receipts.results,
        },
        "task_state_changes": receipts.state_changes,
        "exec_ok": stats.exec_ok,
        "exec_failed": stats.exec_failed,
    }


def build_record(run: RunInputs) -> dict[str, object]:
    agent_id = run.agent_id
    spawner, status, last_message_text = run.meta
    prompts = _prompt_fields(spawner, run.inbounds, run.transcript)

    stats = _event_stats(run.events)
    compactions = sum(1 for name in run.log_events if name == "compact")

    plugins_activated, plugins_activated_skipped = _plugins_activated(run.events)

    record: dict[str, Any] = {
        "agent_id": agent_id,
        "week": run.week,
        "spawner": spawner,
        "model": _mode(stats.models),
        **prompts,
        "transcript": [{"type": row.type, "content": row.content} for row in run.transcript],
        "final_output": last_message_text or "",
        "turns": stats.turns,
        "tokens_in": stats.tokens_in,
        "tokens_out": stats.tokens_out,
        "tools_called": count_tool_calls(stats.code_bodies),
        "skills_touched": list(run.skills_touched),
        "plugins_activated": plugins_activated,
        "plugins_activated_skipped": plugins_activated_skipped,
        "exec_ok": stats.exec_ok,
        "exec_failed": stats.exec_failed,
        "last_exec_failed": stats.last_exec_failed,
        "compactions": compactions,
        "terminated": status == "terminated",
        "duration_s": stats.duration_s,
    }
    if run.leak_paths is not None:
        record.update(_leak_fields(stats.code_bodies, agent_id, run.leak_paths))
    record["evidence"] = _evidence_block(run, stats, prompts, record["final_output"])
    record["evidence_grade"], record["unknown_reason"] = _grade(record["evidence"])
    record["schema_version"] = 2
    record["label"] = label(record)
    return record
