# meiva:note
# `meiva_code._state_access` is the surface's reach into its own plugin state. A stateful child's slot
# is lazy: it exposes `materialize()`, which loads the agent-runtime face and binds the real state.
# `read_state` / `update_state` must call it before resolving the handle, and must refuse to run
# outside an exec turn.
# meiva:link meiva_builtins/plugins/meiva_code/docs/meiva_code.meiva.okf.md
from collections.abc import Iterator

import pytest

from agent.state import build_agent_state
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install, process_context
from meiva_builtins.plugins.meiva_code import _state_access, agent_runtime
from meiva_builtins.plugins.meiva_code.tests.surface_support import code_registry


@pytest.fixture(autouse=True)
def _installed_surface() -> Iterator[None]:
    _ = install.install(
        code_registry(),
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=fixed_sampling_owner(),
    )
    yield
    _ = install.uninstall()
    process_context.unbind_exec_turn()


# meiva:note
# A slot that binds the real state only when asked, like the child's lazy slot.
class _LazySlot:
    def __init__(self, paths: set[str]) -> None:
        state_cls = build_agent_state(
            ExtensionRegistry((("meiva_code", agent_runtime.contribute()),))
        )
        self._state = state_cls.model_validate(
            {"messages": [], "halted": False, "meiva_code__injected_paths": paths}
        )
        self.materialized = 0

    def materialize(self) -> None:
        self.materialized += 1
        process_context.set_state(self._state)


def test_read_state_materializes_the_lazy_slot_first() -> None:
    slot = _LazySlot({"foo.py"})
    process_context.set_state(slot)
    process_context.set_state_update({})

    assert _state_access.read_state().injected_paths == {"foo.py"}
    assert slot.materialized == 1


def test_update_state_materializes_the_lazy_slot_and_records_the_delta() -> None:
    slot = _LazySlot({"foo.py"})
    process_context.set_state(slot)
    process_context.set_state_update({})

    _state_access.update_state({"injected_paths": {"bar.py"}})

    assert slot.materialized == 1
    assert _state_access.read_state().injected_paths == {"bar.py"}


def test_state_access_outside_an_exec_turn_raises() -> None:
    with pytest.raises(process_context.PluginStateOutsideTurnError, match=r"read\(\)"):
        _ = _state_access.read_state()
