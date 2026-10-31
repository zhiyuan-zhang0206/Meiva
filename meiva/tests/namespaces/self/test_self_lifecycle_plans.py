# meiva:note
# `meiva/self/lifecycle_plans.py` unit tests: whether the harness a process runs under may take a
# lifecycle action (on the context, an attached capture and the stored row), whether the caller is
# an attached controller, the overlay a restart stores with the receipt of a withdrawn model, and
# the texts of a compaction. Asserted by value; the entries against a real database are covered by
# `test_self_lifecycle.py`.
# meiva:link meiva/self/docs/self.meiva.okf.md

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from base.agents.context.identity import AgentIdentity, ExternalLease
from base.lm.catalog import ModelCatalog
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from base.packages.tests.model_catalog import AddModels
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from meiva.self.lifecycle_plans import (
    HarnessFacts,
    compact_notice,
    harness_refusal,
    heartbeat_pause_attributes,
    is_attached_controller,
    restart_overlay,
    stored_harness_refusal,
)

_NATIVE = HarnessFacts(
    context_harness="meiva", capture_attached=False, capture_registered_harness=None
)
_LEASE = ExternalLease(agent_id=9, validate=lambda: 9, config=lambda: None)


@pytest.mark.parametrize("action", ["attach", "compact", "restart", "terminate", "pause_heartbeat"])
def test_the_native_harness_may_take_every_action(action: str) -> None:
    assert harness_refusal(action, _NATIVE) is None


@pytest.mark.parametrize("action", ["attach", "compact", "restart"])
def test_an_external_harness_context_is_refused_the_actions_the_native_loop_owns(
    action: str,
) -> None:
    facts = replace(_NATIVE, context_harness="claude-code")
    refused = harness_refusal(action, facts)
    assert refused is not None
    assert f"meiva.self.{action}()" in refused
    assert "'claude-code'" in refused


@pytest.mark.parametrize("action", ["terminate", "pause_heartbeat"])
def test_an_external_harness_may_still_terminate_and_pause(action: str) -> None:
    assert harness_refusal(action, replace(_NATIVE, context_harness="claude-code")) is None


def test_an_attached_capture_is_judged_on_the_harness_it_registered() -> None:
    facts = replace(_NATIVE, capture_attached=True, capture_registered_harness="codex")
    refused = harness_refusal("restart", facts)
    assert refused is not None
    assert "'codex'" in refused


def test_an_attached_capture_that_registered_no_harness_is_external() -> None:
    facts = replace(_NATIVE, capture_attached=True, capture_registered_harness=None)
    refused = harness_refusal("compact", facts)
    assert refused is not None
    assert "'external'" in refused


def test_a_capture_that_is_not_attached_is_not_judged() -> None:
    facts = replace(_NATIVE, capture_attached=False, capture_registered_harness="codex")
    assert harness_refusal("restart", facts) is None


def test_the_context_is_judged_before_the_capture() -> None:
    facts = HarnessFacts("claude-code", capture_attached=True, capture_registered_harness="codex")
    refused = harness_refusal("restart", facts)
    assert refused is not None
    assert "'claude-code'" in refused
    assert "'codex'" not in refused


def test_a_missing_row_refuses_nothing() -> None:
    assert stored_harness_refusal("restart", None) is None


def test_the_stored_harness_is_judged_like_the_others() -> None:
    assert stored_harness_refusal("restart", ("meiva",)) is None
    refused = stored_harness_refusal("restart", ("codex",))
    assert refused is not None
    assert "'codex'" in refused
    assert stored_harness_refusal("terminate", ("codex",)) is None


def test_a_row_with_no_harness_is_judged_by_its_text() -> None:
    refused = stored_harness_refusal("restart", (None,))
    assert refused is not None
    assert "'None'" in refused


@pytest.mark.parametrize(
    ("identity", "attached"),
    [
        (None, False),
        (AgentIdentity(agent_id=7, owns_loop=True), False),
        (AgentIdentity(agent_id=7, owns_loop=False, lease=_LEASE), False),
        (AgentIdentity(agent_id=7, owns_loop=True, lease=_LEASE), True),
    ],
)
def test_only_a_lease_holder_that_owns_the_loop_is_an_attached_controller(
    identity: AgentIdentity | None, attached: bool
) -> None:
    assert is_attached_controller(identity) is attached


def _withdrawn(catalog: ModelCatalog, add_models: AddModels) -> tuple[str, ModelCatalog]:
    name = "glm-vision-fixture"
    row = replace(
        catalog.models["glm-5.3"],
        spawnable=False,
        unavailable_fallback="glm-5.3",
        media_types=frozenset({"image"}),
    )
    return name, add_models(catalog, {name: row})


def test_a_registered_model_is_stored_as_given(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    overlay: dict[str, object] = {"llm_model": "gpt-5.6-sol"}
    stored = restart_overlay(overlay, configs=None, catalog=model_catalog, home=tmp_path)
    assert dict(stored.overlay) == {"llm_model": "gpt-5.6-sol"}
    assert stored.receipt is None


def test_a_withdrawn_model_is_stored_as_its_fallback_with_a_receipt_and_the_input_is_kept(
    tmp_path: Path, model_catalog: ModelCatalog, add_models: AddModels
) -> None:
    name, catalog = _withdrawn(model_catalog, add_models)
    overlay: dict[str, object] = {"llm_model": name}
    stored = restart_overlay(overlay, configs=None, catalog=catalog, home=tmp_path)
    assert dict(stored.overlay) == {"llm_model": "glm-5.3"}
    assert stored.receipt == (name, "glm-5.3")
    assert overlay == {"llm_model": name}


def test_an_unregistered_model_is_refused_before_anything_is_stored(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    with pytest.raises(InvalidConfigOverlay, match="not a registered model"):
        restart_overlay(
            {"llm_model": "no-such-model"}, configs=None, catalog=model_catalog, home=tmp_path
        )


def test_a_key_no_config_owns_is_refused(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    with pytest.raises(InvalidConfigOverlay):
        restart_overlay({"no_such_field": 1}, configs=None, catalog=model_catalog, home=tmp_path)


def test_exec_timeout_seconds_overlay_is_stored_and_restorable_by_explicit_value(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    tightened = restart_overlay(
        {"exec_timeout_seconds": 25.0}, configs=None, catalog=model_catalog, home=tmp_path
    )
    assert dict(tightened.overlay) == {"exec_timeout_seconds": 25.0}
    assert tightened.receipt is None

    restored = restart_overlay(
        {"exec_timeout_seconds": 300.0}, configs=None, catalog=model_catalog, home=tmp_path
    )
    assert dict(restored.overlay) == {"exec_timeout_seconds": 300.0}
    assert restored.receipt is None


@pytest.mark.parametrize(
    "bad_timeout",
    [0, 0.0, -5.0, float("nan"), float("inf"), 1200.0, 1500.0, None, "25s"],
)
def test_exec_timeout_seconds_overlay_rejects_non_positive_unbounded_or_invalid_values(
    tmp_path: Path, model_catalog: ModelCatalog, bad_timeout: object
) -> None:
    with pytest.raises(InvalidConfigOverlay, match="exec_timeout_seconds"):
        restart_overlay(
            {"exec_timeout_seconds": bad_timeout},
            configs=None,
            catalog=model_catalog,
            home=tmp_path,
        )


# meiva:note
# The plugin configs an overlay is checked against are the ones installed in the home the caller
# passed: this test installs a probe plugin in its own home only, so an overlay of the field it
# declares resolves there and is unknown to the process's own home.
def test_the_overlay_is_checked_against_the_plugin_configs_of_the_home_handed_in(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    plugin_dir = tmp_path / "plugins" / "restart_probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text('__description__ = "restart probe"\n', encoding="utf-8")
    (plugin_dir / "default_config.py").write_text(
        """from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class ProbeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    restart_probe: str = Field(default="unset", json_schema_extra={"per_agent": True})


def contribute() -> PluginContributions:
    return PluginContributions(config=ProbeConfig)
""",
        encoding="utf-8",
    )

    stored = restart_overlay(
        {"restart_probe": "x"}, configs=None, catalog=model_catalog, home=tmp_path
    )
    assert dict(stored.overlay) == {"restart_probe": "x"}

    with pytest.raises(InvalidConfigOverlay, match="not in framework Settings nor any plugin"):
        restart_overlay(
            {"restart_probe": "x"},
            configs=None,
            catalog=model_catalog,
            home=tmp_path / "another-home",
        )


def test_a_compaction_reports_only_the_length_of_its_summary() -> None:
    notice = compact_notice("abc" * 10)
    assert notice.audit_payload == {"compact_kind": "summary", "length": 30}
    assert notice.request_content == "[compact requested, 30 chars]"
    assert "abc" not in notice.request_content


def test_the_pause_event_reports_whole_seconds() -> None:
    assert heartbeat_pause_attributes(89.6) == {"duration_s": 90}
    assert isinstance(heartbeat_pause_attributes(89.6)["duration_s"], int)
