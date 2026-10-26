# meiva:note
# Explicit resources used by the operation-root tests.

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any, TypedDict

from langchain_core.messages import AIMessage

from base.agents.history.hierarchy.generate import InvocableLlm
from base.agents.history.hierarchy.group import Group
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys

_NODE_LINE = re.compile(r"\[id (\d+)\] [^\n]+\n([^\n]*)")


class LmWiring(TypedDict):
    catalog: ModelCatalog
    keys: ProviderKeys
    llm_override: str


def lm_wiring(catalog: ModelCatalog) -> LmWiring:
    return {"catalog": catalog, "keys": inert_keys(catalog), "llm_override": ""}


def consumer_config() -> ConfigBoot:
    config = snapshot_process_config()
    config.set_field("llm_model", "deepseek-flash")
    config.set_field("hierarchy_model", "deepseek-flash")
    return config


class GroupModels:
    def __init__(self, catalog: ModelCatalog, state: dict[str, Any]) -> None:
        self.catalog = catalog
        self.state = state
        self.gets: list[tuple[str, ModelOverrides, str]] = []

    def get(self, model: str, overrides: ModelOverrides, reasoning: str = "") -> InvocableLlm:
        self.gets.append((model, overrides, reasoning))
        return self

    def invoke(self, messages: list[Any], /) -> AIMessage:
        prompt = str(messages[0].content)
        pairs = [(int(i), text) for i, text in _NODE_LINE.findall(prompt)]
        ids = [i for i, _ in pairs]
        self.state.setdefault("asked", []).append(ids)
        self.state.setdefault("prompts", []).append(prompt)
        if "pause" in self.state:
            time.sleep(float(self.state["pause"]))
        if "barrier" in self.state:
            self.state["barrier"].wait()
        if "error" in self.state:
            raise self.state["error"]
        if "reply_text" in self.state:
            return AIMessage(content=str(self.state["reply_text"](pairs)))
        groups: Sequence[Group] = self.state["reply"](pairs) if "reply" in self.state else ()
        text = "".join(
            f'<group first="{g.first}" last="{g.last}">{g.summary}</group>' for g in groups
        )
        return AIMessage(content=text or "no groups")
