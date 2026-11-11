from __future__ import annotations

from pydantic import BaseModel

from base.api_contracts.question import QuestionSpec


class OpenQuestion(BaseModel):
    id: int
    title: str
    content: str | None
    question: QuestionSpec
