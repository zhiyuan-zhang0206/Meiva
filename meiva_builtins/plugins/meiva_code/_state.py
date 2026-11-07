from __future__ import annotations

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class MeivaCodeState(BaseModel):
    # meiva:note
    # plugins.meiva_code persistent state — survives across turns/restarts via LangGraph checkpoint.
    #
    # - messages: declared base channel (exact BaseAgentState annotation) —
    # the context-file notes this plugin appends during the exec turn; the
    # exec node merges them into its own messages delta (after the exec
    # result). See the field comment below.
    # - injected_paths: set of context-file (AGENTS.md / CLAUDE.md) paths already
    # surfaced to the agent, deduped to prevent the wrap from re-injecting
    # (both auto-inject and direct agent reads mark into here; see module
    # docstring + `_wrapped_read` comment).
    # - last_seen_compact: bookmark compared against the built-in
    # `compact.version`. After compact strips messages, injected_paths is
    # lazily cleared (detected at wrap entry, not actively reset) — the same
    # monotonic version-counter reset `meiva_sdk_reminder` uses.
    # meiva:link meiva_builtins/plugins/meiva_code/docs/meiva_code.meiva.okf.md

    # Base-channel declaration: this plugin legitimately appends system notes
    # (AGENTS.md / CLAUDE.md context injection) to the framework's `messages`
    # channel during the exec turn. The annotation must match BaseAgentState
    # exactly (incl. the add_messages reducer) — the registry's state
    # validation enforces it — and the exec node merges the plugin's messages delta with
    # its own ToolMessage delta (agent/graph/exec/node.py), so the notes ride in
    # the same in-memory state update instead of a side-channel file.
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list[AnyMessage])
    injected_paths: set[str] = Field(default_factory=set)
    injected_hashes: set[str] = Field(default_factory=set)
    last_seen_compact: int = 0
