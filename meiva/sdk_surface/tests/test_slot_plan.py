# meiva:note
# `meiva/sdk_surface/pure/plans/installation/slot_plan.py` and `attempt_plan.py` unit tests: what
# the installation slot of the `meiva` module becomes after the faces loading, a skill root
# mounted or unmounted, a plugin config overlay and a load attempt, and what a failed load left
# in it. Asserted by value on `Installation` values; nothing here touches the module. The slot
# writers are covered by the install and skill-source tests.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from base.agents.sdk import InertSamplingPolicy
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from meiva.sdk_surface.installation import (
    Installation,
    Loading,
    NamespaceAdded,
    SkillProvider,
)
from meiva.sdk_surface.pure.plans.installation.attempt_plan import (
    attempt_cleared,
    attempt_marked,
    load_failure,
)
from meiva.sdk_surface.pure.plans.installation.slot_plan import (
    SlotWrite,
    faces_loaded,
    mounted,
    overlaid,
    unmounted,
)


def _provider(name: str) -> SkillProvider:
    root = Path(name)
    return lambda: [root]


def _installation() -> Installation:
    return Installation.bare(sampling=InertSamplingPolicy())


def test_a_recorded_failure_is_what_a_failed_load_leaves() -> None:
    failure = RuntimeError("plugin import failed")
    assert load_failure(Loading(None, failure)) is failure


@pytest.mark.parametrize("slot", [None, Loading(), Loading(_installation()), _installation()])
def test_no_recorded_failure_is_no_failure(slot: Installation | Loading | None) -> None:
    assert load_failure(slot) is None


def test_faces_are_recorded_once() -> None:
    first = faces_loaded(_installation())
    assert first is not None
    assert first.faces
    assert faces_loaded(first) is None


def test_faces_need_an_installation() -> None:
    assert faces_loaded(None) is None


def test_anything_beyond_mounted_skill_roots_keeps_an_installation_when_the_last_provider_goes() -> (
    None
):
    mine = _provider("a")
    mounted_value = mounted(None, mine)
    registry = ExtensionRegistry((("p", PluginContributions()),))
    base = Installation.bare_provider_mount(mine)
    for extra in (
        replace(base, registry=registry),
        replace(base, expansions=("x",)),
        replace(base, wrap_layers=MappingProxyType({"files.read": ()})),
        replace(base, metered=((object(), "read"),)),
        replace(base, disabled=frozenset({"monitor"})),
        replace(base, faces=True),
        replace(base, undo=(NamespaceAdded("ns", ModuleType("ns"), True),)),
    ):
        write = unmounted(None, mounted_value, extra, mine)
        assert write is not None
        assert isinstance(write.value, Installation), extra
        assert write.value.skill_providers == ()


def test_mounting_appends_to_an_installation() -> None:
    first, second = _provider("a"), _provider("b")
    prior = replace(_installation(), skill_providers=(first,))
    after = mounted(prior, second)
    assert after.skill_providers == (first, second)
    assert after.sampling is prior.sampling


@pytest.mark.parametrize("prior", [None, Loading(), Loading(_installation())])
def test_mounting_without_an_installation_makes_a_bare_provider_mount(
    prior: Loading | None,
) -> None:
    provider = _provider("a")
    after = mounted(prior, provider)
    assert after.skill_providers == (provider,)
    assert not after.registry.plugins
    assert not after.undo
    assert not after.faces


def test_unmounting_an_untouched_slot_puts_the_prior_value_back() -> None:
    provider = _provider("a")
    prior = _installation()
    mounted_value = mounted(prior, provider)
    assert unmounted(prior, mounted_value, mounted_value, provider) == SlotWrite(prior)
    assert unmounted(None, mounted_value, mounted_value, provider) == SlotWrite(None)


def test_unmounting_from_an_installation_replaced_since_drops_only_this_provider() -> None:
    mine, other = _provider("a"), _provider("b")
    mounted_value = mounted(None, mine)
    replaced = replace(_installation(), skill_providers=(other, mine))
    write = unmounted(None, mounted_value, replaced, mine)
    assert write is not None
    assert isinstance(write.value, Installation)
    assert write.value.skill_providers == (other,)


def test_the_last_provider_leaves_nothing_behind_of_a_bare_mount() -> None:
    mine = _provider("a")
    mounted_value = mounted(None, mine)
    later_bare = Installation.bare_provider_mount(mine)
    assert unmounted(None, mounted_value, later_bare, mine) == SlotWrite(None)


def test_the_last_provider_of_a_real_installation_leaves_the_installation() -> None:
    mine = _provider("a")
    mounted_value = mounted(None, mine)
    real = replace(_installation(), skill_providers=(mine,), expansions=("shell",))
    write = unmounted(None, mounted_value, real, mine)
    assert write is not None
    assert isinstance(write.value, Installation)
    assert write.value.skill_providers == ()
    assert write.value.expansions == ("shell",)


@pytest.mark.parametrize("current", [None, Loading(), Loading(_installation())])
def test_a_slot_that_holds_no_installation_is_left_alone(current: Loading | None) -> None:
    provider = _provider("a")
    assert unmounted(None, mounted(None, provider), current, provider) is None


class _Config(BaseModel):
    marker: str = "disk"


def test_an_overlay_replaces_the_configs_with_a_read_only_view() -> None:
    current = _installation()
    configs = {"demo": _Config(marker="agent")}
    after = overlaid(current, configs)
    config = after.configs["demo"]
    assert isinstance(config, _Config)
    assert config.marker == "agent"
    assert after.sampling is current.sampling
    view: Any = after.configs
    with pytest.raises(TypeError):
        view["other"] = _Config()


def test_marking_an_attempt_wraps_the_installation_the_slot_holds() -> None:
    installation = _installation()
    write = attempt_marked(installation)
    assert write is not None
    assert isinstance(write.value, Loading)
    assert write.value.installation is installation


def test_marking_an_attempt_on_an_empty_slot_wraps_nothing() -> None:
    write = attempt_marked(None)
    assert write is not None
    assert isinstance(write.value, Loading)
    assert write.value.installation is None


def test_an_attempt_already_marked_is_not_marked_again() -> None:
    assert attempt_marked(Loading()) is None


def test_clearing_an_attempt_gives_the_slot_back_to_the_installation_it_wrapped() -> None:
    installation = _installation()
    assert attempt_cleared(Loading(installation)) == SlotWrite(installation)
    assert attempt_cleared(Loading()) == SlotWrite(None)


def test_clearing_where_no_attempt_is_marked_writes_nothing() -> None:
    assert attempt_cleared(None) is None
    assert attempt_cleared(_installation()) is None
