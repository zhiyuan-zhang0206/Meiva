from __future__ import annotations

from pydantic import Field

from base.config.base import EnvSettings


class AgentSkillRecallSettings(EnvSettings):
    skill_recall_enabled: bool = Field(
        title="Suggest relevant skills before a turn",
        default=True,
        alias="MEIVA_SKILL_RECALL",
        description=(
            "Before each turn woken by a message from a person or by a schedule, score every "
            "skill in the agent's index against the recent conversation and name the best "
            "matches in a system note, so a relevant playbook is offered without the agent "
            "having to match its index by hand. A skill the agent already read, was already "
            "offered, or has preloaded is not offered again until the context is compacted. "
            "Off makes no model call."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_model: str = Field(
        title="Skill suggestion decision model",
        default="gemini-decision-1",
        alias="MEIVA_SKILL_RECALL_MODEL",
        description=(
            "Decision model that scores the skill index for skill recall: one request per "
            "inbound-woken turn, one yes/no question per skill. Must be a decision model "
            "(not a chat model). Calibrated 2026-10-11 by benchmark together with the question "
            "wording, the skill description format and the conversation template skill recall "
            "sends, and skill_recall_threshold is a score on this model's scale: recalibrate "
            "before changing any of them."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_threshold: float = Field(
        title="Minimum relevance for skill suggestions",
        default=0.35,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_THRESHOLD",
        description=(
            "Lowest probability of 'yes' at which a skill is offered on person- or "
            "schedule-woken turns. Calibrated 2026-11-18 on gemini-decision-1 for recall-first "
            "coverage: an extra one-line suggestion costs ~35 tokens and can be ignored, whereas "
            "missing an applicable workflow, goal, or verification skill causes rework."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_peer_threshold: float = Field(
        title="Minimum relevance for peer-message skill suggestions",
        default=0.45,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_PEER_THRESHOLD",
        description=(
            "Lowest probability of 'yes' at which a skill is offered on a turn woken by "
            "another agent's message, instead of skill_recall_threshold. Peer traffic carries "
            "more coordination noise than direct user turns, so its cut stays stricter than "
            "skill_recall_threshold while favoring recall-first coverage (re-thresholding the "
            "historical 999-message peer_recall_replay.json score fixture from 0.70 to 0.45 "
            "raises coverage across its 428 work dispatches from 63.3% to 98.6%). Only used "
            "when skill_recall_peer_messages is on."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_max: int = Field(
        title="Maximum suggested skills per turn",
        default=5,
        ge=0,
        alias="MEIVA_SKILL_RECALL_MAX",
        description=(
            "The most skills one recall note offers. Set to 5 so domain implementation skills "
            "and tie-breaking do not crowd out general workflow, goal-contract, or verification "
            "skills under recall-first scoring, while bounding prompt overhead to at most five "
            "summary lines (~180 tokens). Ties on the score are broken by skill identifier, so "
            "the choice is deterministic. 0 offers nothing."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_deadline_seconds: float = Field(
        title="Skill suggestion timeout (seconds)",
        default=3.0,
        gt=0,
        alias="MEIVA_SKILL_RECALL_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget (seconds) for one skill-recall pass. Recall runs before the LLM "
            "on every person- or schedule-woken turn, so a slow decision provider must not hold "
            "the turn: when the deadline expires the turn proceeds with no skill note and the "
            "next one tries again. Independent of memory_recall_deadline_seconds."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_peer_messages: bool = Field(
        title="Suggest skills for peer messages",
        default=True,
        alias="MEIVA_SKILL_RECALL_PEER_MESSAGES",
        description=(
            "Also consider turns woken by another agent's message, not only by a person or a "
            "schedule. A peer message is scored only when the peer gate passes: one cheap "
            "decision request checks whether the message asks for concrete work rather than "
            "reporting status or acknowledging receipt. Off keeps the 2026-10-11 behaviour "
            "exactly; the peer gate exists because most inbound messages are peer traffic."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_peer_gate_work_floor: float = Field(
        title="Peer message work request threshold",
        default=0.30,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_WORK_FLOOR",
        description=(
            "Peer gate: skip a peer message when the probability that it asks the recipient to "
            "do work is at or below this floor, i.e. when it is clearly a status report, "
            "acknowledgement or notification. Uncertain messages above this floor pass and are "
            "scored rather than dropped (96% recall on the 999-message peer benchmark), because "
            "a missed offer costs a redo while an extra offer costs one line in the prompt."
        ),
        json_schema_extra={
            "help_text": "Skip messages whose probability of requesting work is at or below this threshold.",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_peer_gate_max_chars: int = Field(
        title="Peer message screening limit (characters)",
        default=4000,
        gt=0,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_MAX_CHARS",
        description=(
            "Peer gate: only the first this-many characters of the peer message are sent to the "
            "decision model. The gate reads one message, never the conversation, which is what "
            "makes it about 15x cheaper than the recall pass it guards on typical messages "
            "(about 3x at this cap)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_peer_gate_deadline_seconds: float = Field(
        title="Peer message screening timeout (seconds)",
        default=3.0,
        gt=0,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget (seconds) for the peer gate. Matched to "
            "skill_recall_deadline_seconds (3.0s) so transient provider tail latency does not "
            "drop peer skill recall before scoring."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )
