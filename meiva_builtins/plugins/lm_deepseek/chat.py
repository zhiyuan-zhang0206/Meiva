"""DeepSeek chat transport with native Responses structured output.

DeepSeek's Anthropic endpoint supports only `output_config.effort`, so JSON-schema
output must use its Responses endpoint. Ordinary chat and tools retain the
Anthropic transport; this selects a wire API for the same model, never a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypeGuard

from langchain_core.language_models import LanguageModelInput
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic

DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_ANTHROPIC_API_BASE = f"{DEEPSEEK_API_BASE}/anthropic"
_STRUCTURED_OUTPUT = "with_structured_output"


class _StructuredOutputFactory(Protocol):
    def __call__(
        self,
        schema: dict[str, object] | type[object],
        *,
        include_raw: bool = False,
        method: Literal["function_calling", "json_schema"] = "function_calling",
        **kwargs: object,
    ) -> Runnable[LanguageModelInput, dict[str, object] | BaseModel]: ...


def _is_structured_output_factory(fn: object) -> TypeGuard[_StructuredOutputFactory]:
    return callable(fn)


# meiva:note
# The effort `build()` stored under `extra_body.output_config.effort`, or None when any level of that
# path is absent or not the shape `build()` writes.
def _wire_effort(model_kwargs: Mapping[str, object]) -> str | None:
    extra_body = model_kwargs.get("extra_body")
    if not is_object_dict(extra_body):
        return None
    output_config = extra_body.get("output_config")
    if not is_object_dict(output_config):
        return None
    effort = output_config.get("effort")
    return effort if isinstance(effort, str) else None


class DeepSeekChatModel(ThinkingTokensChatAnthropic):
    """Keep Anthropic chat; use Responses for native JSON-schema output."""

    def with_structured_output(
        self,
        schema: dict[str, object] | type[object],
        *,
        include_raw: bool = False,
        method: Literal["function_calling", "json_schema"] = "function_calling",
        http_client: object = None,
        http_async_client: object = None,
        **kwargs: object,
    ) -> Runnable[LanguageModelInput, dict[str, object] | BaseModel]:
        """Use native schema output with optional caller-owned HTTP clients."""
        if method != "json_schema":
            if http_client is not None or http_async_client is not None:
                raise ValueError("Custom HTTP clients require method='json_schema'")
            build = object_of(getattr, super(), _STRUCTURED_OUTPUT)
            if not _is_structured_output_factory(build):
                raise TypeError("super().with_structured_output is not callable")
            return build(schema, include_raw=include_raw, method=method, **kwargs)

        from langchain_openai import ChatOpenAI

        effort = _wire_effort(self.model_kwargs)
        if self.thinking is not None and self.thinking.get("type") == "disabled":
            effort = "none"
        model = ChatOpenAI(
            model=self.model,
            api_key=self.anthropic_api_key,
            base_url=DEEPSEEK_API_BASE,
            use_responses_api=True,
            reasoning_effort=effort,
            timeout=self.default_request_timeout,
            max_retries=self.max_retries,
            max_completion_tokens=self.max_tokens,
            temperature=self.temperature,
            http_client=http_client,
            http_async_client=http_async_client,
        )
        kwargs.setdefault("strict", True)
        build = object_of(getattr, model, _STRUCTURED_OUTPUT)
        if not _is_structured_output_factory(build):
            raise TypeError("model.with_structured_output is not callable")
        return build(schema, method="json_schema", include_raw=include_raw, **kwargs)
