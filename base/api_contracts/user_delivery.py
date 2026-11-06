from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UserDeliveryMode(StrEnum):
    NAIVE = "naive"
    PREFIX = "prefix"
    SDK = "sdk"


class ReplyExpectation(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class PublishMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(max_length=100_000)
    response: ReplyExpectation
    suggestions: list[str] = Field(max_length=3)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("content must be nonblank; use null to declare no user update")
        return value

    @field_validator("suggestions")
    @classmethod
    def valid_suggestions(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("suggestions must be nonblank and at most 500 characters")
        if len({value.strip() for value in values}) != len(values):
            raise ValueError("suggestions must be distinct")
        return values

    @model_validator(mode="after")
    def coherent_delivery(self) -> "PublishMessage":
        if self.response is ReplyExpectation.NONE and self.suggestions:
            raise ValueError("response=none requires suggestions=[]")
        if self.content is None and self.response is not ReplyExpectation.NONE:
            raise ValueError("no update requires response=none and suggestions=[]")
        return self


class MessageResolution(StrEnum):
    ANSWERED = "answered"
    DISMISSED = "dismissed"


class UserMessage(PublishMessage):
    id: int
    agent_id: int
    mode: UserDeliveryMode
    source_key: str
    created_at: datetime
    read_at: datetime | None
    resolved_at: datetime | None
    resolution: MessageResolution | None
    reply: str | None


class MessageAction(StrEnum):
    READ = "read"
    ANSWER = "answer"
    DISMISS = "dismiss"


class ResolveMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: MessageAction
    reply: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def coherent_answer(self) -> "ResolveMessage":
        if self.action is MessageAction.ANSWER:
            if self.reply is None or not self.reply.strip():
                raise ValueError("answer requires a nonblank reply")
        elif self.reply is not None:
            raise ValueError("reply is only accepted for action=answer")
        return self
