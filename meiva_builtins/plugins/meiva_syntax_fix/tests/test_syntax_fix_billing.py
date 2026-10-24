from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from base.lm import usage
from base.lm.catalog import ModelCatalog
from meiva_builtins.plugins.meiva_syntax_fix.tests.hook_support import ScriptedRepairModel


async def test_successful_syntax_repair_call_emits_chat_billing(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
) -> None:
    from meiva_builtins.plugins.meiva_syntax_fix._llm_repair import repair_once

    emitted: list[tuple[AIMessage, dict[str, Any]]] = []
    llm = ScriptedRepairModel(
        replies=["fixed = True"],
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )

    def _emit(message: AIMessage, **kwargs: Any) -> None:
        emitted.append((message, kwargs))

    monkeypatch.setattr(usage, "emit_billing_from_message", _emit)

    assert await repair_once(llm, [], catalog=model_catalog) == "fixed = True"
    assert len(emitted) == 1
    assert emitted[0][0].content == "fixed = True"
    assert emitted[0][0].usage_metadata == llm.usage
    assert emitted[0][1]["model"] == "deepseek-flash"
    assert emitted[0][1]["usage_kind"] == "chat"
    assert emitted[0][1]["catalog"] is model_catalog
