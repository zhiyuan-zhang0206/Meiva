"""The process-local SDK entry: its context and exec-turn state slots, and the contexts built for them.

The execution child, schedule runner and external attachment publish their context
here; ``meiva.context`` / ``meiva.state`` / ``meiva.state_update`` on the ``meiva`` module are attribute
delegations to the slots below. Hosted graph runs receive their context through LangGraph Runtime
and never publish an agent identity: a shared host only records its posture (`bind_host_process`).
The slots are kept in the ``meiva`` module object's own dictionary (as the SDK installation is), so
reloading ``meiva`` neither resets nor duplicates them. This module owns no thread patch or live
clients; the clients a context carries are built lazily by `process_clients`.

Framework-internal. Plugin authors do not touch the slots: exec-side state read/write goes through
`agent.state.PluginStateHandle`, and host-side logic (graph hooks) never touches them at all - it
reads the graph `state` argument and returns an update dict.

Lifecycle of the exec-turn state slot (framework side): before agent code runs, the exec child
(after loading plugins) sets `set_state(<snapshot validated from the request envelope>)` and
`set_state_update({})` (`agent/execution/child.py:build_state_slot`). `PluginStateHandle.read`
reads the state; `.update` synchronously mutates its working copy and accumulates the raw delta
into the update dict. When the child exits it writes the update dict into its result envelope;
exec_node reads it from there and merges it into `Command(update=...)`, going through the
LangGraph reducer. Nothing resets the slots in the child: they are discarded with the process. An
external attachment unbinds them at detach. No other process - the agent host included - ever binds
them. Under the LangGraph cycling topology nodes run sequentially, so there is no cross-turn race;
if parallel branches (fan-out) are introduced, this one-slot model must be re-evaluated.
"""

from __future__ import annotations

from base.agents.context import MeivaContext as MeivaContext
from base.host.boundary.untyped import object_of
from base.native_process.loaded_commit import LoadedCommit

from . import (
    CONTEXT_KEY,
    IMAGE_KEY,
    STATE_KEY,
    UPDATE_KEY,
    meiva_module,
)
from . import (
    ContextOutsideProcessError as ContextOutsideProcessError,
)
from . import (
    PluginStateOutsideTurnError as PluginStateOutsideTurnError,
)
from . import (
    SdkProcessPurpose as SdkProcessPurpose,
)
from . import (
    get_state as get_state,
)
from . import (
    get_state_update as get_state_update,
)
from . import (
    in_exec_turn as in_exec_turn,
)
from . import (
    outside_exec_turn as outside_exec_turn,
)
from . import (
    outside_process as outside_process,
)
from .plugins import (
    bind_context as bind_context,
)
from .plugins import (
    bind_host_process as bind_host_process,
)
from .plugins import (
    is_host_process as is_host_process,
)
from .validation import (
    set_state as set_state,
)
from .validation import (
    set_state_update as set_state_update,
)
from .validation import (
    unbind_exec_turn as unbind_exec_turn,
)

_CONTEXT_KEY = CONTEXT_KEY
_STATE_KEY = STATE_KEY
_UPDATE_KEY = UPDATE_KEY
_IMAGE_KEY = IMAGE_KEY


def _slots() -> dict[str, object]:
    return meiva_module().__dict__


# meiva:note
# A slot that was never written is unbounded, and that is what `None` says. A slot that holds
# anything else was written by something that is not this module, so it raises instead of reading
# as "no context" — the two states have different causes and only one of them is normal.
_MISSING = object()


def _bound() -> MeivaContext | SdkProcessPurpose | None:
    value = _slots().get(CONTEXT_KEY, _MISSING)
    if value is _MISSING:
        return None
    if isinstance(value, (MeivaContext, SdkProcessPurpose)):
        return value
    raise TypeError(
        f"meiva.{CONTEXT_KEY} must be a MeivaContext or an SdkProcessPurpose, "
        f"got {type(value).__name__}"
    )


def bound_context() -> MeivaContext | None:
    """Inspect the existing entry without turning host posture into a context."""
    bound = _bound()
    return None if isinstance(bound, SdkProcessPurpose) else bound


def require_bound_context() -> MeivaContext:
    """The bound context, or `ContextOutsideProcessError` where this process has none.

    The raw slot read behind the `meiva.context` attribute; it never builds a launched context."""
    bound = _bound()
    if bound is SdkProcessPurpose.SHARED_HOST:
        raise ContextOutsideProcessError(
            "the shared agent host has no SDK process context; "
            "host code uses the context its caller supplied"
        )
    if bound is None:
        raise outside_process("context")
    return bound


def current_context() -> MeivaContext:
    """What `meiva.context` answers: the bound context, a launched script's own on its first read,
    or `ContextOutsideProcessError`. Read through the live `meiva` module, which owns that rule."""
    context = object_of(getattr, meiva_module(), "context")
    if not isinstance(context, MeivaContext):
        raise TypeError(f"meiva.context must be a MeivaContext, got {type(context).__name__}")
    return context


def ambient_context() -> MeivaContext | None:
    try:
        return current_context()
    except ContextOutsideProcessError:
        return None


def unbind_context() -> MeivaContext | None:
    """Release the SDK binding without creating a launched-script context.

    The caller owns restoration and closing its clients.
    """
    if is_host_process():
        raise RuntimeError("the shared agent host cannot release its startup posture")
    value = _slots().pop(CONTEXT_KEY, _MISSING)
    if value is _MISSING:
        return None
    if isinstance(value, MeivaContext):
        return value
    raise TypeError(f"meiva.{CONTEXT_KEY} must be a MeivaContext, got {type(value).__name__}")


# meiva:note
# The code image `meiva/__init__.py` captured when the package was imported. The slot is written
# once, by that import, so a value of any other shape means the module was tampered with.
def loaded_code_image() -> LoadedCommit:
    image = _slots().get(IMAGE_KEY)
    if not isinstance(image, LoadedCommit):
        raise TypeError(f"meiva.{IMAGE_KEY} must be a LoadedCommit, got {type(image).__name__}")
    return image
