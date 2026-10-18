from __future__ import annotations

import pytest
from pydantic import ValidationError

from ops.rpc_schemas import AgentMessageIn


@pytest.mark.parametrize("content", ["", "   ", []])
def test_empty_content_is_rejected(content: object) -> None:
    with pytest.raises(ValidationError):
        AgentMessageIn.model_validate({"content": content, "source": "agent:1"})


def test_text_content_is_stripped() -> None:
    message = AgentMessageIn.model_validate({"content": "  hi  ", "source": "agent:1"})
    assert message.content == "hi"
