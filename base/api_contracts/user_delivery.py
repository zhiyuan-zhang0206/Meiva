from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from base.api_contracts.notice_question import NonBlank, NoticeQuestion


class UserDeliveryMode(StrEnum):
    NAIVE = "naive"
    PREFIX = "prefix"
    SDK = "sdk"


class PublishMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(max_length=100_000)
    suggestions: list[str] = Field(default_factory=list, max_length=3)

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
        if self.content is None and self.suggestions:
            raise ValueError("no update requires suggestions=[]")
        return self


class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None
    suggestions: list[str]
    id: int
    agent_id: int
    mode: UserDeliveryMode
    source_key: str
    created_at: datetime
    read_at: datetime | None


class ResolveMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["read"]


class AskMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: NonBlank = Field(max_length=1000)
    content: str | None = Field(default=None, max_length=100_000)
    question: NoticeQuestion
