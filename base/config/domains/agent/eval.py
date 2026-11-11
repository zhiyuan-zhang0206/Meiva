from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from base.config.base import EnvSettings


class AgentEvalSettings(EnvSettings):
    eval_isolation: bool = Field(
        title="Isolate evaluation agents",
        default=False,
        alias="MEIVA_EVAL_ISOLATION",
        description=(
            "Isolate an evaluation agent from shared memory, network-facing SDK "
            "capabilities, and peer-result reads."
        ),
        json_schema_extra={
            "per_agent": True,
            "lifecycle": "frozen",
            "restart_required": "agent",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    eval_network_allowlist: Annotated[list[str], NoDecode] = Field(
        title="Network capabilities allowed during evaluation",
        default_factory=list,
        alias="MEIVA_EVAL_NETWORK_ALLOWLIST",
        description=(
            "Comma-separated network-facing SDK capabilities an isolated evaluation "
            "agent may use: `web` and `understand`."
        ),
        json_schema_extra={
            "per_agent": True,
            "lifecycle": "frozen",
            "restart_required": "agent",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    @field_validator("eval_network_allowlist", mode="before")
    @classmethod
    def _split_eval_network_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("eval_network_allowlist")
    @classmethod
    def _validate_eval_network_allowlist(cls, value: list[str]) -> list[str]:
        unsupported = sorted(set(value) - {"web", "understand"})
        if unsupported:
            raise ValueError(
                "eval network allowlist only accepts 'web' and 'understand'; "
                f"unsupported entries: {unsupported}"
            )
        return value
