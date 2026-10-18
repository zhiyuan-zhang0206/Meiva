from __future__ import annotations

from pydantic import Field

from base.config.base import EnvSettings


class AgentSkillRecallSettings(EnvSettings):
    skill_recall_enabled: bool = Field(
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
        default="jev-1.13.0",
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
        default=0.60,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_THRESHOLD",
        description=(
            "Lowest probability of 'yes' at which a skill is offered. The model's probabilities "
            "are coarse, so this is a cut between 'clearly helps' and everything else rather "
            "than a fine ranking. Calibrated 2026-10-11 with jev-1.13.0 and the current "
            "question wording."
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
        default=0.75,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_PEER_THRESHOLD",
        description=(
            "Lowest probability of 'yes' at which a skill is offered on a turn woken by "
            "another agent's message, instead of skill_recall_threshold. Peer traffic is a "
            "different distribution from person and schedule messages -- most of it is status, "
            "acknowledgement and watcher notices -- so it carries its own cut. Measured "
            "2026-10-18 on 150 real peer messages: 0.60 offers on 112, 0.75 on 50, and every "
            "offer whose playbook would have changed the agent's next action scored at least "
            "0.72. Only used when skill_recall_peer_messages is on."
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
        default=2,
        ge=0,
        alias="MEIVA_SKILL_RECALL_MAX",
        description=(
            "The most skills one recall note offers. Kept small: an offered skill is a "
            "suggestion to spend context on loading its instructions. Ties on the score are "
            "broken by skill identifier, so the choice is deterministic. 0 offers nothing."
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
        default=False,
        alias="MEIVA_SKILL_RECALL_PEER_MESSAGES",
        description=(
            "Also consider turns woken by another agent's message, not only by a person or a "
            "schedule. A peer message is scored only when the peer gate passes: one cheap "
            "decision request checks that the message asks for work and does not already state "
            "how to do it. Off keeps the 2026-10-11 behaviour exactly; the peer gate exists "
            "because most inbound messages are peer traffic that the original trigger excluded."
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
        default=0.30,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_WORK_FLOOR",
        description=(
            "Peer gate: skip a peer message when the probability that it asks the recipient to "
            "do work is at or below this floor, i.e. when it is clearly a status report, "
            "acknowledgement or notification. Messages between this floor and the method "
            "ceiling pass: an uncertain message is scored rather than dropped, because a missed "
            "offer costs a redo while an extra offer costs one line in the prompt."
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

    skill_recall_peer_gate_method_ceiling: float = Field(
        default=0.70,
        gt=0,
        le=1,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_METHOD_CEILING",
        description=(
            "Peer gate: skip a peer message when the probability that it already states how to "
            "do the work (steps, criteria, commands, acceptance checks) is at or above this "
            "ceiling. A brief that carries its own method does not need a playbook offered on "
            "top of it; the same split made the batch-review episodes score 'not applicable' in "
            "the 2026-10-17 evaluation."
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

    skill_recall_peer_gate_max_chars: int = Field(
        default=4000,
        gt=0,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_MAX_CHARS",
        description=(
            "Peer gate: only the first this-many characters of the peer message are sent to the "
            "decision model. The gate reads one message, never the conversation, which is what "
            "makes it about 15x cheaper than the recall pass it guards."
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
        default=1.5,
        gt=0,
        alias="MEIVA_SKILL_RECALL_PEER_GATE_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget (seconds) for the peer gate. It runs before the recall pass, so "
            "a peer-woken turn can spend this budget plus skill_recall_deadline_seconds when "
            "both calls are slow; the gate reads one message (p50 about 0.6s)."
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
