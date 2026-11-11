# meiva:note
# Bind a submitted outcome to the locked question's immutable spec and render what the agent receives.

from typing import NamedTuple

from fastapi import HTTPException

from base.api_contracts.ask import QuestionResult
from base.api_contracts.question import (
    QuestionAnswer,
    answer_reply,
    parse_numbered_answer,
    parse_question,
    validate_answer,
)


class ResolutionAnswer(NamedTuple):
    reply: str
    answer: QuestionAnswer


# meiva:note
# Adapt text-only clients and validate against the authoritative stored options. A skip never
# reaches this function.
def result_answer(result: QuestionResult, raw_question: object) -> ResolutionAnswer:
    question = parse_question(raw_question)
    if result.answer is None and result.reply is None:
        raise TypeError("a skipped result has no answer to settle")
    try:
        if result.answer is not None:
            answer = validate_answer(question, result.answer)
        else:
            answer = parse_numbered_answer(question, result.reply or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"question {result.id}: {exc}") from exc
    return ResolutionAnswer(answer_reply(question, answer), answer)


SKIPPED = "Skipped by the user."


# meiva:note
# One block of the agent's inbound per question, in question id order, each opening with the
# recognisable `Re: question #id "title"` header. A skip is stated as a fact, never as approval.
def reply_block(question_id: int, title: str, reply: str | None) -> str:
    return f'Re: question #{question_id} "{title}"\n\n{SKIPPED if reply is None else reply}'
