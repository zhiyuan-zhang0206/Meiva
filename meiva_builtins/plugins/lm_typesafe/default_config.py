# meiva:link meiva_builtins/plugins/lm_typesafe/docs/systemone/systemone.meiva.okf.md

from __future__ import annotations

from typing import Literal, Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from base.packages.plugins.extensions import PluginContributions


def _validate_endpoint(endpoint: str) -> None:
    if any(char.isspace() for char in endpoint):
        raise ValueError("endpoint must not contain whitespace")
    parts = AnyHttpUrl(endpoint)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not (parts.path or "").endswith("/v1/systemone")
    ):
        raise ValueError(
            "endpoint must be an HTTP(S) /v1/systemone URL without credentials, query or fragment"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("endpoint has an invalid port") from exc
    if port == 0:
        raise ValueError("endpoint port must be positive")


class LocalDecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    wire_model: str = Field(min_length=1)
    response_model: str = Field(min_length=1)
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    context_window: int = Field(gt=0)
    max_request_tokens: int = Field(gt=0)
    question_types: tuple[Literal["noul", "choice", "score"], ...] = Field(
        default=("noul", "choice", "score"), strict=False
    )
    max_choice_options: int = Field(default=100, ge=2, le=255)
    max_score_levels: int = Field(default=10, ge=2, le=10)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        for text in (self.wire_model, self.response_model, self.revision):
            if not text.strip() or text != text.strip():
                raise ValueError(
                    "model identity fields must be non-empty and have no outer whitespace"
                )
        if self.max_request_tokens < self.context_window:
            raise ValueError("max_request_tokens must be >= context_window")
        if not self.question_types or len(set(self.question_types)) != len(self.question_types):
            raise ValueError("question_types must contain unique supported types")
        return self


class LocalDecisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local_endpoint: str = Field(
        default="",
        strict=True,
        json_schema_extra={"scope": "cluster-pinned", "restart_required": "all"},
    )
    local_models: dict[str, LocalDecisionModel] = Field(
        default_factory=dict,
        json_schema_extra={"scope": "cluster-pinned", "restart_required": "all"},
    )

    @model_validator(mode="after")
    def validate_registration(self) -> Self:
        if self.local_models and not self.local_endpoint:
            raise ValueError("local decision models require an explicit endpoint")
        if self.local_endpoint:
            _validate_endpoint(self.local_endpoint)
        for model_id in self.local_models:
            if (
                not model_id.startswith("local-decision-")
                or any(char.isspace() for char in model_id)
                or not model_id.removeprefix("local-decision-").strip()
            ):
                raise ValueError(
                    "local decision model ids must start with local-decision- and name a deployment"
                )
        return self


def contribute() -> PluginContributions:
    return PluginContributions(config=LocalDecisionConfig)
