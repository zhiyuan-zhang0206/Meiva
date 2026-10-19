from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

# meiva:note
# The one thing every external process borrows differently is *how its identity is
# proved*: a takeover lease derived from a session, or a harness registration
# handle. Everything else about an attachment — the boot, the SDK context, the
# state replay, the admission gate, the flush/close ordering — is the same, so the
# kernel asks for these five answers and owns the rest.
#
# `require` is called on every SDK call, not once at entry: a lease that expired or
# a session that closed mid-run must stop the process at its next action, not at
# the next restart.


class IdentitySource(Protocol):
    """What an external attachment needs to know about its borrowed identity."""

    def require(self) -> Mapping[str, Any]:
        """Return the live identity record, or raise when it no longer permits use."""

    def validate(self) -> int:
        """Re-check the identity and return the borrowed agent id."""

    def machine(self) -> str:
        """The machine the identity is bound to; the process must run there."""

    def agent_id(self) -> int:
        """The borrowed agent id."""

    def merge_delta(self, encoded: str, *, expected_version: int) -> int:
        """Persist one plugin-state delta under optimistic version control."""


class AttachmentCodec(Protocol):
    """The agent-state codec: snapshot load, plugin-state schema, delta codec."""

    def load_snapshot(self, agent_id: int) -> tuple[Any, Any, Any]: ...

    def plugin_state_schema(self, state: Any) -> Any: ...

    def encode_plugin_delta(self, update: Mapping[str, Any], state_cls: type[Any]) -> str: ...

    def decode_plugin_delta(self, encoded: str, state_cls: type[Any]) -> Any: ...

    def apply_plugin_delta(self, state: Any, delta: Any) -> None: ...
