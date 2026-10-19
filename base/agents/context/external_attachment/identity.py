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
# the next restart. `merge_delta` is the only write path for plugin state, and it
# takes the expected version so two attachments cannot silently overwrite each
# other.
# meiva:link base/agents/context/external_attachment/docs/external-attachment.meiva.okf.md


class IdentitySource(Protocol):
    def require(self) -> Mapping[str, Any]: ...

    def validate(self) -> int: ...

    def machine(self) -> str: ...

    def agent_id(self) -> int: ...

    def delta_version(self) -> int: ...

    def merge_delta(self, encoded: dict[str, str], *, expected_version: int) -> int: ...


class AttachmentCodec(Protocol):
    def load_snapshot(self, agent_id: int) -> tuple[Any, Any, Any]: ...

    def plugin_state_schema(self, state: Any) -> Any: ...

    def encode_plugin_delta(
        self, update: Mapping[str, Any], state_cls: type[Any]
    ) -> dict[str, str]: ...

    def decode_plugin_delta(self, encoded: dict[str, str], state_cls: type[Any]) -> Any: ...

    def apply_plugin_delta(self, state: Any, delta: Any) -> None: ...
