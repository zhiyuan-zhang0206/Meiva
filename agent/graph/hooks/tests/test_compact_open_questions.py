# meiva:note
# The wind-down reminder before a compaction lists the agent's unanswered questions, with the fact that
# they carry over and the entry that closes one; with none open it is the reminder it always was.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from agent.graph.hooks.compact import compact_reminder_update
from agent.state import AgentState
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.questions.model import OpenQuestion
from base.api_contracts.question import QuestionType, TextQuestion
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY
from base.paths import meiva_home

_MODEL = "deepseek-flash"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)
_TINY = 1e-6


def _agent(**pins: float) -> AgentSlices:
    return AgentSlices.resolve({"llm_model": _MODEL, **pins}, default_reader=defaults())


def _state() -> AgentState:
    messages: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
    ]
    return AgentState(messages=messages)


def _reminder(questions: list[OpenQuestion]) -> str:
    update: dict[str, Any] | None = compact_reminder_update(
        _state(),
        _agent(compact_reminder_fraction=_TINY),
        catalog=build_model_catalog(home=meiva_home()),
        extensions=EMPTY,
        open_questions=questions,
        now=_NOW,
        inputs=TimestampPolicy.off(),
    )
    assert update is not None
    [note] = update["messages"]
    return str(note.content)


def _q(question_id: int, title: str) -> OpenQuestion:
    return OpenQuestion(
        id=question_id, title=title, content=None, question=TextQuestion(type=QuestionType.TEXT)
    )


def test_the_reminder_lists_the_unanswered_questions() -> None:
    text = _reminder([_q(41, "Which database?"), _q(44, "Ship v2 now?")])
    assert (
        'Questions you asked the user that are still unanswered: #41 "Which database?", '
        '#44 "Ship v2 now?".'
    ) in text
    assert "They stay open across compaction and are shown to you again afterwards." in text
    assert "meiva.ui.close_question(id) closes one." in text
    assert "Close any" not in text


def test_the_reminder_without_open_questions_has_no_question_section() -> None:
    assert "unanswered" not in _reminder([])
