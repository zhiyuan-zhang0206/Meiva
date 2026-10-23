# meiva:note
# What the syntax-fix hook tests share: the runtime a hook sees, execute_code calls to hand it, and
# the scripted repair models that the agent's `llm_override` slice names. The repair model is built
# by the real `build_chat_model`; the override path is its own seam for a model a test scripts.
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.runtime import Runtime
from pydantic import Field

from agent.state import AgentState
from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import inert_keys

_MODELS = "meiva_builtins.plugins.meiva_syntax_fix.tests.hook_support"

REPAIRS_UNTERMINATED_STRING = f"{_MODELS}:repairs_unterminated_string"
REPAIRS_AFTER_ONE_RETRY = f"{_MODELS}:repairs_after_one_retry"
NEVER_REPAIRS = f"{_MODELS}:never_repairs"
REPAIRS_IN_A_FENCE = f"{_MODELS}:repairs_in_a_fence"
UNAVAILABLE = f"{_MODELS}:unavailable"
FAILS_TO_ANSWER = f"{_MODELS}:fails_to_answer"
ANSWERS_BLANK = f"{_MODELS}:answers_blank"

REPAIRED = "x = 'fixed'\nprint(x)"


class ScriptedRepairModel(BaseChatModel):
    replies: list[str] = Field(default_factory=list)
    fails: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted-repair"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        if self.fails:
            raise TimeoutError("the repair model did not answer")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])


def repairs_unterminated_string(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    return ScriptedRepairModel(replies=[REPAIRED])


def repairs_in_a_fence(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    return ScriptedRepairModel(replies=[f"```python\n{REPAIRED}\n```"])


def repairs_after_one_retry(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    return ScriptedRepairModel(replies=["x = (", REPAIRED])


def never_repairs(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    return ScriptedRepairModel(replies=["x = ("])


def fails_to_answer(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    return ScriptedRepairModel(fails=True)


def answers_blank(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    return ScriptedRepairModel(replies=["   "])


def unavailable(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    del model, agent_id
    raise RuntimeError("the repair model is not configured")


def agent_slices(*, llm_override: str | None = None, **pins: Any) -> AgentSlices:
    cluster = {} if llm_override is None else {"lm__llm_override": llm_override}
    return AgentSlices.resolve(pins, default_reader=defaults(**cluster))


def make_runtime(*, llm_override: str | None = None, **pins: Any) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            agent=agent_slices(llm_override=llm_override, **pins),
            catalog=build_model_catalog(),
            provider_keys=inert_keys(build_model_catalog()),
            clock_factory=Clock.from_settings,
        )
    )


def execute_code_state(*codes: str) -> AgentState:
    return AgentState(
        messages=[
            AIMessage(
                id="ai-batch",
                content="",
                tool_calls=[
                    {"name": "execute_code", "args": {"code": code}, "id": f"call-{index}"}
                    for index, code in enumerate(codes)
                ],
            )
        ]
    )
