# meiva:note
# What `meiva.self` decides before it acts, over values its facade read: whether the harness the
# process runs under may take a lifecycle action, whether the caller is a controller attached
# through a lease, the overlay a restart stores and the receipt of a withdrawn model it rewrote,
# and the texts of a compaction. The facade (`meiva/self/__init__.py`) reads the bound context, the
# database row and the installation, runs the SQL `plans.py` returns and publishes the wake; these
# functions only choose.
# meiva:link meiva/self/docs/self.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from base.agents.context.identity import AgentIdentity
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from base.packages.plugins.config_registration import validate_config_overlay
from meiva.sdk_surface.pure.caller.identity_plan import native_harness_refusal


# meiva:note
# The harness facts a lifecycle action is judged on: the harness the bound context names ("meiva"
# when none is bound), whether an SDK capture owner holds a live handle (an external harness
# session is attached) and the harness that session registered, as the registration holds it.
@dataclass(frozen=True, slots=True)
class HarnessFacts:
    context_harness: str
    capture_attached: bool
    capture_registered_harness: str | None


# meiva:note
# The refusal an external harness earns for `action` (restart, compact and attach belong to the
# native harness), checked first on the context's harness, then on the attached capture's, which
# is "external" when its registration names none. None when the action may go ahead.
def harness_refusal(action: str, facts: HarnessFacts) -> str | None:
    refused = native_harness_refusal(facts.context_harness, action)
    if refused is not None or not facts.capture_attached:
        return refused
    return native_harness_refusal(facts.capture_registered_harness or "external", action)


# meiva:note
# The refusal the harness stored for the agent earns for `action`, given the row the lookup of
# `agents_meta.harness` returned (None when the agent has no row, then nothing is refused).
def stored_harness_refusal(action: str, row: tuple[object, ...] | None) -> str | None:
    if row is None:
        return None
    return native_harness_refusal(str(row[0]), action)


# meiva:note
# Whether the caller is a controller attached through an external lease that owns the loop: it
# ends its own process instead of raising the agent's termination, and needs no identity check.
def is_attached_controller(identity: AgentIdentity | None) -> bool:
    return identity is not None and identity.owns_loop and identity.lease is not None


# meiva:note
# The overlay a restart stores: the caller's overlay validated against the installed plugin
# configs and the catalog's models, a copy of which has a withdrawn `llm_model` replaced by its
# registered fallback. `receipt` is (requested, resolved) when it did, so the shell can log it.
@dataclass(frozen=True, slots=True)
class StoredOverlay:
    overlay: Mapping[str, object]
    receipt: tuple[str, str] | None


def restart_overlay(
    config_overlay: dict[str, object],
    *,
    configs: Mapping[str, BaseModel] | None,
    catalog: ModelCatalog,
    home: Path,
) -> StoredOverlay:
    validate_config_overlay(
        config_overlay,
        configs,
        models=catalog.models,
        decision_models=catalog.decision_models,
        home=home,
    )
    stored = dict(config_overlay)
    receipt = normalize_overlay_llm_model(stored, models=catalog.models)
    return StoredOverlay(stored, receipt)


# meiva:note
# What a compaction says about itself: the payload of its audit fact and the content of the live
# request the frontend shows. Both name the summary's length only, never its text.
@dataclass(frozen=True, slots=True)
class CompactNotice:
    audit_payload: Mapping[str, object]
    request_content: str


def compact_notice(summary: str) -> CompactNotice:
    return CompactNotice(
        audit_payload={"compact_kind": "summary", "length": len(summary)},
        request_content=f"[compact requested, {len(summary)} chars]",
    )


# meiva:note
# The attributes of the `heartbeat_paused` event: the pause rounded to whole seconds, so the
# emitted metric keeps one kind (task #4011).
def heartbeat_pause_attributes(seconds: float) -> Mapping[str, object]:
    return {"duration_s": round(seconds)}
