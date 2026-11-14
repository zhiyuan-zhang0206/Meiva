# meiva:note
# The wire contract of asking and answering questions: one ask call carries a non-empty list of
# questions, one submission settles every open question of one agent. A question is answered or
# skipped, never left out.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/questions.md

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base.api_contracts.question import NonBlank, QuestionAnswer, QuestionSpec


class AskQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: NonBlank = Field(max_length=1000)
    content: str | None = Field(default=None, max_length=100_000)
    question: QuestionSpec


class AskBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: Annotated[list[AskQuestion], Field(min_length=1)]


class AskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int]


# meiva:note
# An empty result and a legacy explicit skip share the same normalized receipt payload.
# meiva:link gateway/agents/docs/operation-receipts.md
class QuestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)
    answer: QuestionAnswer | None = None
    reply: NonBlank | None = None
    skip: bool = Field(
        default_factory=bool,
        description="Legacy explicit skip. An ID-only result also skips the question.",
    )

    @model_validator(mode="after")
    def normalize_outcome(self) -> Self:
        given = [self.answer is not None, self.reply is not None, self.skip]
        if sum(given) > 1:
            raise ValueError("a question result accepts at most one of answer, reply or skip")
        if not any(given):
            self.skip = True
        return self


# meiva:note
# One submission: the open set the user saw, and an outcome for each of those questions.
class AnswerQuestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_ids: Annotated[list[int], Field(min_length=1)]
    results: Annotated[list[QuestionResult], Field(min_length=1)]

    @model_validator(mode="after")
    def results_cover_expected(self) -> Self:
        if len(set(self.expected_ids)) != len(self.expected_ids):
            raise ValueError("expected_ids must be distinct")
        if sorted(result.id for result in self.results) != sorted(self.expected_ids):
            raise ValueError("results must contain exactly one outcome for each expected id")
        return self
