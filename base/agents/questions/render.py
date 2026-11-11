# meiva:note
# What an agent is told about its unanswered questions, in three places: a line list for the
# pre-compaction reminder, the full list for the context a compaction rebuilds, and the same list for
# an external harness after its CLI compacted. The text states the facts and the one entry that acts
# on them; it does not instruct. Lengths follow the limits the per-call delivery note used for the
# same fields: a title and a body are cut at their own caps and a list shows at most twenty.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/ask.md

from __future__ import annotations

from collections.abc import Sequence

from base.agents.questions.model import OpenQuestion
from base.api_contracts.question import ChoiceQuestion, numbered_options

TITLE_CHARS = 1000
CONTENT_CHARS = 800
LIST_CAP = 20

_CLOSE = "meiva.ui.close_question(id) closes one."
_ANSWER = 'An answer arrives as a message that starts "Re: question #<id>".'


def _more(count: int) -> list[str]:
    return [f"{count - LIST_CAP} more are open."] if count > LIST_CAP else []


def reminder_text(questions: Sequence[OpenQuestion]) -> str:
    if not questions:
        return ""
    shown = ", ".join(f'#{q.id} "{q.title[:TITLE_CHARS]}"' for q in questions[:LIST_CAP])
    parts = [
        f"Questions you asked the user that are still unanswered: {shown}.",
        *_more(len(questions)),
        "They stay open across compaction and are shown to you again afterwards. " + _CLOSE,
    ]
    return " ".join(parts)


def _detail(question: OpenQuestion) -> str:
    lines = [f'#{question.id} "{question.title[:TITLE_CHARS]}"']
    if question.content:
        lines.append(question.content[:CONTENT_CHARS])
    spec = question.question
    if isinstance(spec, ChoiceQuestion):
        lines.append(f"Type: {spec.type.value}. Options:\n{numbered_options(spec)}")
    else:
        lines.append("Type: text.")
    return "\n".join(lines)


def reinjection_text(questions: Sequence[OpenQuestion], *, source: str) -> str:
    if not questions:
        return ""
    entries = [_detail(q) for q in questions[:LIST_CAP]]
    return "\n\n".join(
        [
            f"Questions you asked the user that are still unanswered ({source}):",
            *entries,
            *_more(len(questions)),
            f"{_ANSWER} {_CLOSE}",
        ]
    )
