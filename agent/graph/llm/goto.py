"""The node names the llm node may route to."""

from typing import Literal

LlmGoto = Literal["before_turn", "exec", "init_context", "claim"]
