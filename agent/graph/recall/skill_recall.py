# meiva:note
# Skill recall: offer the skills that could plausibly help with the conversation's latest request.
#
# The standing `# Capabilities` index asks the agent to match every task against it by hand, and an
# index of dozens of skills is easy to skim past. Before a turn woken by a person's message, a
# schedule, or a work-bearing peer message, one decision-model request scores every candidate skill
# against the recent conversation and the top matches are named in a system note.
#
# Recall-first policy (2026-11-18): an extra offered skill costs one summary line (~35 tokens) and
# the agent simply ignores an unhelpful match, whereas missing a relevant workflow, goal-contract,
# or verification playbook causes silent quality and process regressions. Both the question wording
# and the thresholds therefore favor recall over stingy filtering.
#
# One request, one question per skill. The conversation is sent once as the state and every skill
# adds only its own yes/no question (about 150 tokens each), so the request grows with the catalog.
# A catalog too large for the decision model's request limit raises `DecisionInputTooLargeError`;
# that is logged and nothing is offered. The state is never truncated and the catalog is never
# chunked here: a partial score over some of the skills would offer the best of an arbitrary subset,
# and a request split by chunks would multiply the cost of every inbound turn.
#
# A removable layer, like passive memory recall: a decision failure of any kind returns `None`
# and the turn goes on without the note.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import NamedTuple

from langchain_core.messages import AnyMessage, HumanMessage

from agent.graph.prompt.capabilities import RecallCandidate, index_entry
from agent.graph.recall.conversation_query import build_conversation_query
from agent.messages import NoteTag, system_note_message
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import DecisionError, DecisionInputTooLargeError, YesNo
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.decision.call import decide
from base.log import logger

_LABEL = "skill-recall"

# What the decision model is shown as the state: this preamble, then the recent conversation
# (`build_conversation_query`). The model reads the last message as the request to judge.
STATE_PREAMBLE = (
    "Recent conversation of an AI agent (oldest first; the last message is the latest "
    "request or trigger):\n\n"
)

# meiva:note
# The recall-first question, asked once per skill. `{identifier}` and `{description}` are the
# skill's display identifier and its index-line description; the note the agent reads spells the
# loadable path and `SKILL.md` file path instead (`render_note`).
QUESTION_TEMPLATE = (
    "Skill `meiva.skills.{identifier}` -- {description}\n\n"
    "Could loading this skill help the agent handle, scope, execute, or verify the "
    "latest request in the conversation?"
)
TRUE_IF = (
    "Yes: the skill's domain playbook, workflow, goal-contract guidance, or verification "
    "method is plausibly relevant to scoping, executing, or checking this request (prefer "
    "recalling a potentially helpful skill over missing it)."
)
FALSE_IF = (
    "No: the skill is clearly unrelated to the request's domain and work shape (for example "
    "a pure status acknowledgement, casual chat, or an unrelated integration)."
)

# Agent-visible framing. Observational, no implementation detail (obeys the SDK docstring
# discipline since the agent reads this text). It stays close to the drift note's framing
# because the lines below it are the same index lines.
_FRAMING = (
    "Skills that match the current conversation, offered automatically. The descriptions "
    "are summaries; read a skill's `SKILL.md` file at the path shown (or inspect sub-skills "
    "with `meiva.help(meiva.skills.<path>)`) before using the skill."
)


class SkillRecall(NamedTuple):
    """Result of a skill recall pass.

    note: the system-styled human message to inject.
    identifiers: the display identifiers of the skills it names -- the caller records these
        so the same skill is not offered again this context window.
    """

    note: HumanMessage
    identifiers: set[str]


def skill_questions(candidates: Sequence[RecallCandidate]) -> dict[str, YesNo]:
    """One calibrated yes/no question per candidate, keyed by the skill's display identifier."""
    return {
        c.identifier: YesNo(
            question=QUESTION_TEMPLATE.format(identifier=c.identifier, description=c.description),
            true_if=TRUE_IF,
            false_if=FALSE_IF,
        )
        for c in candidates
    }


def pick_skills(scores: Mapping[str, float], *, threshold: float, limit: int) -> list[str]:
    """The identifiers scoring at least `threshold`, best first, at most `limit`.

    The model's probabilities are coarse, so equal scores are common; they are ordered by
    identifier, which makes the choice deterministic."""
    qualified = [identifier for identifier, score in scores.items() if score >= threshold]
    qualified.sort(key=lambda identifier: (-scores[identifier], identifier))
    return qualified[:limit]


def render_note(
    candidates: Sequence[RecallCandidate],
    *,
    inputs: TimestampPolicy,
    created_at: datetime,
) -> HumanMessage:
    """The system-styled note naming `candidates`, one index line each."""
    lines = [index_entry(c.target, c.description, skill_md=c.skill_md or None) for c in candidates]
    return system_note_message(
        content=f"{_FRAMING}\n\n" + "\n".join(lines),
        tag=NoteTag.SKILL_RECALL,
        picked_up_at=created_at,
        created_at=created_at,
        inputs=inputs,
    )


async def recall_skills(
    messages: Sequence[AnyMessage],
    candidates: Sequence[RecallCandidate],
    *,
    inputs: TimestampPolicy,
    agent: AgentSlices,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    agent_id: int,
    created_at: datetime,
    peer_turn: bool = False,
) -> SkillRecall | None:
    """Score `candidates` against the recent conversation and render the best matches as a
    note to inject, or `None` when there is nothing to offer.

    `None` when: the conversation yields no query, there are no candidates, no candidate
    reaches the threshold, or the decision call failed. A failure is logged, never raised: the
    caller is a before_llm hook, so an exception here would end the agent process rather than
    just the recall. The caller applies the deadline and has already removed the skills that
    must not be offered (read, offered, preloaded)."""
    settings = agent.skill_recall
    query = build_conversation_query(messages)
    if not query or not candidates:
        return None
    by_identifier = {c.identifier: c for c in candidates}
    try:
        decisions = await decide(
            settings.skill_recall_model,
            STATE_PREAMBLE + query,
            skill_questions(candidates),
            catalog=catalog,
            keys=keys,
            usage_kind="skill_recall",
            for_agent_id=agent_id,
            timeout=settings.skill_recall_deadline_seconds,
            now=lambda: created_at,
        )
    except DecisionInputTooLargeError as exc:
        # Not a transient failure: this agent's catalog does not fit one request, so recall
        # stays off for it until the catalog shrinks or the model's limit grows.
        logger.warning(
            "[{label}] {body}",
            label=_LABEL,
            body=f"skip: {len(candidates)} candidate skills do not fit one request ({exc})",
            event="skill_recall",
        )
        return None
    except DecisionError as exc:
        logger.warning(
            "[{label}] {body}",
            label=_LABEL,
            body=f"skip: decision call failed ({type(exc).__name__}: {exc})",
            event="skill_recall",
        )
        return None
    scores = {identifier: d.probability("true") for identifier, d in decisions.items()}
    threshold = (
        settings.skill_recall_peer_threshold if peer_turn else settings.skill_recall_threshold
    )
    picked = pick_skills(scores, threshold=threshold, limit=settings.skill_recall_max)
    logger.info(
        "[{label}] {body}",
        label=_LABEL,
        body=f"scored {len(candidates)} skills, offering {picked or 'none'}",
        event="skill_recall",
        candidates=len(candidates),
        offered=picked,
    )
    if not picked:
        return None
    return SkillRecall(
        note=render_note(
            [by_identifier[identifier] for identifier in picked],
            created_at=created_at,
            inputs=inputs,
        ),
        identifiers=set(picked),
    )


# meiva:note
# A peer-woken turn is scored only when this single-question gate passes: the message must ask
# for work rather than merely reporting status or acknowledging receipt. Uncertain answers pass,
# because a missed offer costs a redo while an extra offer costs one line. Delegated briefs that
# include steps or acceptance criteria still pass so workers receive workflow and verification
# skills (the former second question `method_given` cut recall from 96% to 78.3% on the 999-message
# benchmark and was removed). The gate reads one message, never the conversation.
# meiva:link agent/graph/docs/skill-recall.md
PEER_GATE_PREAMBLE = "One message another agent sent to this agent:\n\n"

PEER_GATE_WORK = YesNo(
    question=(
        "Does this message ask the recipient to do a concrete piece of work -- a new task, "
        "a fix, a review, an investigation, a delivery or a decision?"
    ),
    true_if="Yes: the message asks the recipient to act or decide.",
    false_if="No: the message only reports status, acknowledges, confirms, notifies or chats.",
)


# meiva:note
# `True` when the message may ask for work; `False` for a clear status/acknowledgement message
# or a failed decision call. A gate failure falls back to the behaviour before peer messages
# were considered, so a broken gate never changes what an agent sees.
# meiva:link agent/graph/docs/skill-recall.md
async def peer_gate_passes(
    text: str,
    *,
    agent: AgentSlices,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    agent_id: int,
    now: Callable[[], datetime],
) -> bool:
    settings = agent.skill_recall
    stripped = text.strip()
    if not stripped:
        return False
    try:
        decisions = await decide(
            settings.skill_recall_model,
            PEER_GATE_PREAMBLE + stripped[: settings.skill_recall_peer_gate_max_chars],
            {"work": PEER_GATE_WORK},
            catalog=catalog,
            keys=keys,
            usage_kind="skill_recall_peer_gate",
            for_agent_id=agent_id,
            timeout=settings.skill_recall_peer_gate_deadline_seconds,
            now=now,
        )
    except DecisionError as exc:
        logger.warning(
            "[{label}] {body}",
            label=_LABEL,
            body=f"peer gate failed, no skill note this turn ({type(exc).__name__}: {exc})",
            event="skill_recall",
        )
        return False
    work = decisions["work"].probability("true")
    passed = work > settings.skill_recall_peer_gate_work_floor
    logger.info(
        "[{label}] {body}",
        label=_LABEL,
        body=f"peer gate {'passes' if passed else 'skips'} (work={work:.2f})",
        event="skill_recall",
    )
    return passed
