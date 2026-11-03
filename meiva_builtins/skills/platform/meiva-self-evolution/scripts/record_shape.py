# meiva:note
# The shape of one self-evolution dataset record, in one owner: `record.build_record` fills the
# body, `window_records.annotated_record` adds the code-body counters, and every reader of a
# record holds this type instead of `dict[str, Any]` (`label`, `rubric`, `mine`, `scan_report`,
# `daily_scan`, `evaluate`). It is pure data: these types say what a record carries and nothing
# else. The one check of a record read from a file happens where that file is read
# (`mine._read_record`), so the modules that decide over an already-built record import the shape
# alone and pay nothing for it.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
from __future__ import annotations

from typing import NotRequired, TypedDict

from record_inputs import TranscriptLine


# meiva:note
# One peer message the label counted as corrective, and the agent that sent it.
class PeerFeedback(TypedDict):
    from_agent: int
    content: str


# meiva:note
# One attempted result-separation bypass found in the run's executed code.
class LeakAuditEntry(TypedDict):
    surface: str
    evidence: str
    tool: str | None


# meiva:note
# The agent's task receipts in the window, by the request field each one carried.
class TaskUpdates(TypedDict):
    note: int
    status: int
    results: int


# meiva:note
# What the record is based on (`record._evidence_block`). Absent from datasets collected before
# `schema_version` 2, which is why `evidence_grade` is optional on the record.
class Evidence(TypedDict):
    telemetry_events: int
    audit_events: int
    tokens_in: int
    tokens_out: int
    turns: int
    task_prompt_chars: int
    prompt_source: str
    final_output_chars: int
    terminal_status: str | None
    inbound_total: int
    inbound_claimed: int
    task_updates: TaskUpdates
    task_state_changes: int
    exec_ok: int
    exec_failed: int


# meiva:note
# A record before its outcome tier is known: everything `record.build_record` writes except
# `label`, which is decided from these fields.
class RecordBody(TypedDict):
    agent_id: int
    week: str
    spawner: str
    model: str
    task_prompt: str
    task_prompt_source: NotRequired[str]
    followup_prompts: list[str]
    corrections: list[str]
    peer_feedback: list[PeerFeedback]
    transcript: list[TranscriptLine]
    final_output: str
    turns: int
    tokens_in: int
    tokens_out: int
    tools_called: dict[str, int]
    skills_touched: list[str]
    plugins_activated: dict[str, int]
    plugins_activated_skipped: int
    exec_ok: int
    exec_failed: int
    last_exec_failed: bool
    compactions: int
    terminated: bool
    duration_s: float
    builtin_help_calls: NotRequired[int]
    builtin_help_on_meiva: NotRequired[int]
    subprocess_calls: NotRequired[int]
    leak_audit: NotRequired[list[LeakAuditEntry]]
    invalidated: NotRequired[bool]
    evidence: NotRequired[Evidence]
    evidence_grade: NotRequired[str]
    unknown_reason: NotRequired[str | None]
    schema_version: NotRequired[int]
    # meiva:note
    # Fields a dataset from before `schema_version` 2 carries and no current reader uses. They stay
    # named here so that reading such a file keeps every field it read (`exec_duration_s` is also
    # the duration column `mine.exec_duration_stats` still offers).
    exec_duration_s: NotRequired[float]
    breached: NotRequired[bool]
    builtin_help_on_ava: NotRequired[int]


# meiva:note
# A record as a dataset file carries it: the body plus the outcome tier `label.label` decided.
class Record(RecordBody):
    label: str
