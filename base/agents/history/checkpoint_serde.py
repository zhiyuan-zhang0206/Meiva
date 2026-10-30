"""Static checkpoint msgpack allowlist — the framework types that cross the
LangGraph checkpoint serde as pydantic-v2 ext objects.

Lives in `base` so both the agent runner (`agent/state.py` extends it with
dynamically registered plugin state classes) and the gateway cold-load reader
(`base/agents/history/checkpoint.py`) can build their `JsonPlusSerializer` allowlist
without crossing the import layering (`base` may not import `agent`).

LangGraph's `JsonPlusSerializer` deserializes a type only when it is named in
`allowed_msgpack_modules` (or in its built-in safe set). Without an explicit
allowlist the serializer runs permissive and warns on every checkpoint load —
"Deserializing unregistered type agent.state.* ... This will be blocked in a
future version" — once per type per process start; a future langgraph blocks
unregistered types outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import ormsgpack
from langgraph.checkpoint.serde._msgpack import SAFE_MSGPACK_TYPES
from langgraph.checkpoint.serde.jsonplus import (
    EXT_CONSTRUCTOR_KW_ARGS,
    EXT_CONSTRUCTOR_POS_ARGS,
    EXT_CONSTRUCTOR_SINGLE_ARG,
    EXT_DELTA_SNAPSHOT,
    EXT_METHOD_SINGLE_ARG,
    EXT_PYDANTIC_V1,
    EXT_PYDANTIC_V2,
)

# (module, name) pairs. The nested sub-states of `agent.state_channels` are the
# only framework channel values serialized as pydantic-v2 ext objects today.
# Keep in sync with `agent/state.py` — `agent.state.checkpoint_msgpack_allowlist`
# starts from this set.
STATIC_CHECKPOINT_MSGPACK_TYPES: frozenset[tuple[str, str]] = frozenset(
    {
        ("agent.state_channels", "AttachState"),
        ("agent.state_channels", "AttachEntry"),
        ("agent.state_channels", "CompactState"),
        ("agent.state_channels", "MemoryState"),
        ("agent.state_channels", "ContextReset"),
        ("agent.state_channels", "CapabilitiesState"),
        ("agent.state_channels", "CircuitState"),
        ("agent.state_channels", "SkillRecallState"),
        # A list-channel element, not a sub-state: `BaseAgentState.security_findings`.
        ("base.agents.messages.security_finding", "SecurityFindingEntry"),
    }
)

# meiva:note
# What a reader can load is both sets, not the allowlist alone: the ext hook tests
# LangGraph's own safe set first (`_check_allowed`), so messages, datetimes and the
# graph's control types deserialize without ever being named here. A census that
# compared against `STATIC_CHECKPOINT_MSGPACK_TYPES` alone would call every stored
# `HumanMessage` an unknown type and red every drill.
READER_CHECKPOINT_MSGPACK_TYPES: frozenset[tuple[str, str]] = (
    frozenset((module, name) for module, name in SAFE_MSGPACK_TYPES)
    | STATIC_CHECKPOINT_MSGPACK_TYPES
)


# meiva:note
# A class the checkpoint store once held and this build no longer carries. The registry
# exists so a type census can tell apart two facts a bare allowlist check fuses: a name it
# cannot load because the class was deliberately deleted (residue, and a fault only if the
# reader can build it again), and a name it cannot load because nothing ever taught it that
# class (a fault on its face). Without it, either every historical blob reds the check
# forever, or the dead names get added back to the allowlist and the signal disappears.
#
# `retired_in` is the commit that deleted the class, so an entry can be re-checked against
# history. `retired_at` is when that commit landed (UTC) and is **context only**: a census
# must not red on "written after this instant", because a host that still runs pre-deletion
# code keeps writing the class (2026-10-30: rows nine hours past the deletion, written by a
# tree 58 commits behind).
@dataclass(frozen=True)
class RetiredCheckpointType:
    module: str
    name: str
    retired_in: str
    retired_at: datetime
    note: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.name)


RETIRED_CHECKPOINT_MSGPACK_TYPES: tuple[RetiredCheckpointType, ...] = (
    RetiredCheckpointType(
        module="base.agents.incarnation.native_work_models",
        name="NativeWorkTarget",
        retired_in="970632db49",
        retired_at=datetime(2026, 10, 20, 11, 52, 25, tzinfo=UTC),
        note=(
            "The lifecycle pointer's target. Deleted with the pointer and its applied/observed "
            "stages; 31,532 `native_work` blobs and 33,676 writes still name it (2026-10-30)."
        ),
    ),
    RetiredCheckpointType(
        module="base.agents.incarnation.native_work_models",
        name="NativeCancelMarker",
        retired_in="970632db49",
        retired_at=datetime(2026, 10, 20, 11, 52, 25, tzinfo=UTC),
        note=(
            "The cancellation marker of the same retired lifecycle; nine `native_cancel` rows "
            "name it (2026-10-30)."
        ),
    ),
)

_DECODE_OPTIONS = ormsgpack.OPT_NON_STR_KEYS

# meiva:note
# Ext codes whose payload starts `(module, name, ...)`: the four constructor forms, the
# method call (`module, name, arg, method`) and both pydantic forms. The numpy array and
# the delta snapshot are not type envelopes — but a delta snapshot wraps a value that may
# itself hold envelopes, so it is descended into.
_ENVELOPE_EXT_CODES = frozenset(
    {
        EXT_CONSTRUCTOR_SINGLE_ARG,
        EXT_CONSTRUCTOR_POS_ARGS,
        EXT_CONSTRUCTOR_KW_ARGS,
        EXT_METHOD_SINGLE_ARG,
        EXT_PYDANTIC_V1,
        EXT_PYDANTIC_V2,
    }
)
_NESTED_VALUE_EXT_CODES = frozenset({EXT_DELTA_SNAPSHOT})


# meiva:note
# Every `(module, name)` the msgpack `blob` names in an ext envelope. Reads names only: the
# hook never imports a module and never constructs the object it names, so a blob holding a
# class this build deleted still yields the name the reader would have failed on — that is
# the point. Byte matching is not an alternative: the same characters inside a message body
# are text (2026-10-30: a `position(...)` probe found 20 such "hits" in `messages`, all
# prose; decoding found none). Raises `ormsgpack.MsgpackDecodeError` for a blob that is not
# decodable msgpack, which is a finding for the caller, not an empty answer.
def observed_checkpoint_types(blob: bytes) -> frozenset[tuple[str, str]]:

    found: set[tuple[str, str]] = set()

    def hook(code: int, data: bytes) -> object:
        _observe_ext(found, code, data)
        return None

    ormsgpack.unpackb(blob, ext_hook=hook, option=_DECODE_OPTIONS)
    return frozenset(found)


# meiva:note
# Record the type names inside one ext payload. The nested payloads are decoded with this
# same hook, so an envelope inside a constructor argument or inside a delta snapshot is read
# exactly where the reader would reach it.
def _observe_ext(found: set[tuple[str, str]], code: int, data: bytes) -> None:

    def hook(nested_code: int, nested_data: bytes) -> object:
        _observe_ext(found, nested_code, nested_data)
        return None

    if code in _ENVELOPE_EXT_CODES:
        payload: Any = ormsgpack.unpackb(data, ext_hook=hook, option=_DECODE_OPTIONS)
        name = _envelope_type_name(payload)
        if name is not None:
            found.add(name)
    elif code in _NESTED_VALUE_EXT_CODES:
        ormsgpack.unpackb(data, ext_hook=hook, option=_DECODE_OPTIONS)


# meiva:note
# The `(module, name)` at the head of an ext payload, when it has one: the four constructor
# forms and the method form all start `module, name, …`, while another ext code's shape or a
# truncated write names no class.
def _envelope_type_name(payload: object) -> tuple[str, str] | None:
    if not isinstance(payload, list | tuple):
        return None
    items: list[object] = [*payload]
    if len(items) < 3:
        return None
    module, name = items[0], items[1]
    if isinstance(module, str) and isinstance(name, str):
        return (module, name)
    return None


CheckpointTypeStatus = Literal["allowed", "retired", "unknown"]


def checkpoint_type_status(
    key: tuple[str, str],
    *,
    allowed: frozenset[tuple[str, str]] = READER_CHECKPOINT_MSGPACK_TYPES,
    retired: tuple[RetiredCheckpointType, ...] = RETIRED_CHECKPOINT_MSGPACK_TYPES,
) -> CheckpointTypeStatus:
    if key in allowed:
        return "allowed"
    if any(entry.key == key for entry in retired):
        return "retired"
    return "unknown"


# meiva:note
# A stored type name is loadable, retired, or one nothing accounts for — the three answers the
# census sorts every name it decodes into.
# meiva:note
# The registry entry for `key`, if it is a retired type.
def retired_type(key: tuple[str, str]) -> RetiredCheckpointType | None:
    for entry in RETIRED_CHECKPOINT_MSGPACK_TYPES:
        if entry.key == key:
            return entry
    return None
