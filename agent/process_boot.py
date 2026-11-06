from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    # meiva:note
    # Annotation-only: this module is on the exec child's start path
    # (`agent/tests/execution/test_lazy_child_imports.py` probes that importing
    # it leaves the LM stack unloaded), while `boot_agent_scope`'s answer is the
    # caller's `AgentRuntime.llm`. The model itself is built by
    # `base.lm.factory` at the call site. Registered in
    # `[type-checking.files]`.
    from langchain_core.language_models import BaseChatModel

import meiva
from base.api_contracts.agent_contract import Harness
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.db import Database
from base.host.boundary.untyped import object_of
from base.host.env.agent_slices import ModelOverrides, agent_setting
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import ProviderBinding
from base.lm.catalog.provider_keys import ProviderKeys
from base.log import logger
from base.paths import workspace_dir
from base.telemetry.observability import endpoint_override_is_explicit
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import TraceReadiness
from meiva.sdk_surface import sdk_disable


# meiva:note
# The tuple twin of `base.host.boundary.json_values.is_object_list`: the registry hands a list
# setting back as a tuple (`agent_slices._value`), and a TypeGuard is what turns `object` into a
# sequence of `object` instead of a sequence of `Unknown`.
def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list | tuple)


# meiva:note
# A per-agent setting the config registry declares as a list of names. `agent_setting` answers
# `object` (its reader is the SDK config authority), so a value of another shape stops the boot
# naming the setting instead of iterating a string into single characters.
def _names(value: object, setting: str) -> list[str]:
    if not _is_sequence(value):
        raise TypeError(f"{setting} must be a list of names, got {type(value).__name__}")
    names = [item for item in value if isinstance(item, str)]
    if len(names) != len(value):
        raise TypeError(f"{setting} must hold only names")
    return names


# meiva:note
# The plugin namespace `meiva.memory`, or None on a home that never loaded the memory plugin.
# `meiva` answers a plugin name through a module `__getattr__` typed `Any` (the name exists only at
# run time), so the namespace arrives as `object` and each attribute is named at its own use.
def _memory_namespace() -> object | None:
    try:
        return object_of(getattr, meiva, "memory")
    except AttributeError:
        return None


def apply_per_agent_sdk_disable(*, default_reader: Callable[[str, str], object]) -> None:
    configured = agent_setting("sdk_disable", default_reader=default_reader)
    from base.agents.messages.reply_intent import delivery_disabled_entries

    mode = agent_setting("user_delivery_mode", default_reader=default_reader)
    if not isinstance(mode, str):
        raise TypeError("user_delivery_mode must be a string")
    sdk_disable.apply_sdk_disable(
        [*_names(configured, "sdk_disable"), *delivery_disabled_entries(mode)]
    )


def apply_per_agent_eval_isolation(
    *, agent_id: int | None, default_reader: Callable[[str, str], object], home: Path
) -> None:
    if not agent_setting("eval_isolation", default_reader=default_reader):
        return

    allowed_network = set(
        _names(
            agent_setting("eval_network_allowlist", default_reader=default_reader),
            "eval_network_allowlist",
        )
    )
    disabled = ["agents.get_last_message", "tasks", "mcps", "ui"]
    if "web" not in allowed_network:
        disabled.append("web")
    if "understand" not in allowed_network:
        disabled.append("understand")
    sdk_disable.apply_sdk_disable(disabled)

    if agent_id is None:
        raise RuntimeError("eval isolation needs the agent identity of the exec it isolates")
    isolated_pool = workspace_dir(agent_id, home=home) / "memory-pool"
    isolated_pool.mkdir(parents=True, exist_ok=True)
    memory = _memory_namespace()
    if memory is not None:
        # meiva:note
        # The pool's documented help text is carried over verbatim: `meiva.memory.PATH` is a
        # `const(...)` whose `__doc__` is the body `help()` renders, and an isolated run must not
        # describe a different directory than the one it points at.
        doc = object_of(getattr, object_of(getattr, memory, "PATH"), "__doc__")
        if not isinstance(doc, str):
            raise TypeError("meiva.memory.PATH carries no help text to carry over")
        # meiva:note
        # `memory` is `object` here (`meiva` answers plugin names as `Any`), so the two writes are
        # `setattr` with a constant name — the shape `B010` warns about, and the only shape that
        # type-checks against an opaque namespace.
        setattr(memory, "PATH", meiva.const(isolated_pool, doc=doc))  # noqa: B010
        setattr(memory, "search", _isolated_memory_search)  # noqa: B010


# meiva:note
# The isolated pool answers every recall with nothing: the notes it would find live in the cluster
# pool this run must not read. Typed `object` because the plugin that owns `search` declares the
# element type (`MemoryMatch`), and the agent core does not import a plugin.
def _isolated_memory_search(_query: str, _k: int = 5) -> list[object]:
    return []


# meiva:note
# The trace configuration of a booted process: its observability settings, and whether its
# launch environment names the collector endpoint (which opens export on a home that is not
# the observability station).
def process_trace_config(config: ConfigBoot) -> TraceConfig:
    return TraceConfig.of(
        config.view.observability,
        endpoint_explicit=endpoint_override_is_explicit(
            "MEIVA_TELEMETRY_OTLP_ENDPOINT", config.process_environment()
        ),
    )


def init_process_scope(
    *,
    machine: MachineIdentityReader,
    home: Path,
    trace: TraceConfig,
    environ: MutableMapping[str, str],
    readiness: TraceReadiness,
) -> None:
    from base.telemetry.tracing import initialize_tracing

    environ.update(trace.traceloop_environment())
    initialize_tracing(machine=machine, home=home, config=trace, readiness=readiness)


def land_cluster_extensions(db: Database, *, home: Path) -> None:
    from base import paths
    from base.packages.extensions import materialize

    try:
        with db.connect() as conn:
            result = materialize.materialize_skills(
                conn,
                dest_root=paths.skills_dir(home),
                last_written=partial(materialize.last_written_hash, home=home),
            )
    except Exception as exc:
        logger.warning(
            "[extensions] could not read the cluster registry at boot ({}); this "
            "machine keeps whatever skills it already has and retries on the next start",
            exc,
        )
        return
    if result.changed:
        logger.info(
            "[extensions] landed {} skill(s), updated {}",
            len(result.landed),
            len(result.updated),
        )


async def boot_agent_scope(
    agent_id: int,
    llm_model: str,
    overrides: ModelOverrides,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    home: Path,
    harness: Harness = Harness.MEIVA,
) -> tuple[BaseChatModel | None, ProviderBinding | None]:
    workspace = workspace_dir(agent_id, home=home)

    from base.agents.to_user.document import ensure_document

    ensure_document(workspace)

    from .startup import notify_desktop_permissions_at_startup

    await notify_desktop_permissions_at_startup(home)

    if harness is not Harness.MEIVA:
        return None, None

    from base.lm.factory import build_chat_model_bound

    return build_chat_model_bound(
        llm_model,
        agent_id=agent_id,
        overrides=overrides,
        meter=False,
        catalog=catalog,
        keys=keys,
        llm_override=llm_override,
    )
