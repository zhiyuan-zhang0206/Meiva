"""Identity derived from the SDK's local context or an explicit host context.

The execution child, launched script and exclusive external attachment initialize
``meiva.context``. Host code passes its own ``MeivaContext``; native turn metadata is
never an SDK identity source. Borrowed leases are validated on each identity use.
"""

from __future__ import annotations

from base.agents.context.identity import AgentIdentity
from meiva.sdk_surface import process_context
from meiva.sdk_surface.process_context import MeivaContext
from meiva.sdk_surface.pure.caller.identity_plan import (
    ActorFacts,
    actor_after_external,
    actor_before_external,
    attributed_agent_id,
    caller_facts,
    native_harness_refusal,
    self_action_refusal,
)
from meiva.sdk_surface.pure.caller.usage_plan import actor_id_refusal, lease_refusal

from .installation import installed
from .validation import is_launched_child as is_launched_child


def _bound(context: MeivaContext | None = None) -> AgentIdentity | None:
    """Read an explicit host context or the SDK's process-local entry."""
    context = context if context is not None else process_context.ambient_context()
    return None if context is None else context.identity


def _harness_name() -> str:
    resolved = process_context.ambient_context()
    if resolved is None:
        return "meiva"
    if resolved.harness.value != "meiva":
        return resolved.harness.value
    if (
        resolved.sdk_capture is not None
        and getattr(resolved.sdk_capture, "handle", None) is not None
    ):
        reg = getattr(resolved.sdk_capture, "_registration", None)
        return str(getattr(reg, "harness", None) or "external")
    return resolved.harness.value


def validate_external_identity(context: MeivaContext | None = None) -> int | None:
    """Recheck an attached lease; no-op for the existing native runtime paths."""
    identity = _bound(context)
    return identity.lease.validate() if identity is not None and identity.lease else None


def agent_id(context: MeivaContext | None = None) -> int | None:
    """Resolve the agent id used to attribute this process's work.

    A validated borrowed identity takes precedence over the supplied or local
    context's identity. An invalid borrowed lease raises
    instead of falling back. Returns `None` when no source provides an identity;
    callers that tolerate this pre-bootstrap state must check for it explicitly.
    """
    return attributed_agent_id(validate_external_identity(context), caller_facts(_bound(context)))


def require_agent_id(context: MeivaContext | None = None) -> int:
    """Return this process's agent id, or raise if it has none.

    Use this at every call site that stamps the agent id into durable data
    (spawner / message source / etc.) — it fails fast instead of letting
    ``None`` leak into the database as the malformed string ``"agent:None"``.

    Raises:
        RuntimeError: no context carrying an agent id is bound and
        ``MEIVA_AGENT_ID`` is not set in the environment.
    """
    resolved = agent_id(context)
    if resolved is None:
        if context is None:
            from meiva.sdk_surface import install as sdk_install

            sdk_install.raise_load_failure()
        raise RuntimeError(
            "this process has no established agent identity — "
            "meiva.agents.spawn / send_message / get_last_message require "
            "a bootstrapped agent process, or MEIVA_AGENT_ID in the environment "
            "of a shell session launched by one"
        )
    return resolved


def require_lease_free_agent_id() -> int:
    """Require an agent context without a borrowed lease for remote admission.

    A live ExternalLease is an in-process callback, not an HTTP credential or
    serializable authority. Remote compound acceptance cannot revalidate it
    after its own transaction lock wait. This guard makes no server ACL claim.
    """
    refused = lease_refusal(caller_facts(_bound()))
    if refused is not None:
        raise ValueError(refused)
    actor = require_agent_id()
    invalid = actor_id_refusal(actor)
    if invalid is not None:
        raise invalid[0](invalid[1])
    return actor


def require_actor(context: MeivaContext | None = None) -> str:
    """Return this process's asserted provenance, validating a borrowed lease first.

    A borrowed `agent:<id>` identity takes precedence. A supplied host context
    is authoritative; without one, an explicit external tool profile precedes
    the local SDK context's actor and agent identity. An invalid borrowed lease
    raises instead of falling back. These provenance channels do not replace
    the gateway's credential checks.

    Use at every call site that stamps provenance into durable data (spawner /
    message source). Fails fast if neither a system actor nor an agent id was
    established, instead of letting a malformed ``agent:None`` leak into the DB.

    Raises:
        RuntimeError: no actor or agent identity is available, or the borrowed
            lease no longer permits this identity.
    """
    facts = ActorFacts(
        borrowed_agent_id=validate_external_identity(context),
        context_supplied=context is not None,
        identity=caller_facts(_bound(context)),
    )
    early = actor_before_external(facts)
    if early is not None:
        return early
    installation = installed()
    external = None if installation is None else installation.external_caller
    decision = actor_after_external(facts, None if external is None else external.source())
    if decision.actor is not None:
        return decision.actor
    if decision.recheck_load_failure:
        from meiva.sdk_surface import install as sdk_install

        sdk_install.raise_load_failure()
    raise RuntimeError(
        "this process has no established actor or agent identity — "
        "meiva.agents.spawn / send_message / resurrect need one "
        "(a bootstrapped agent process, or a context with an actor for a system principal)"
    )


# meiva:note
# Refuse a native-only `meiva.self` action when running on an external harness.
def _assert_native_harness(action: str) -> None:
    refused = native_harness_refusal(_harness_name(), action)
    if refused is not None:
        raise RuntimeError(refused)


def assert_self_action(action: str) -> None:
    """Refuse a lifecycle self-action unless this process is a bootstrapped agent
    process — one that both owns the turn loop and has an established identity.

    The identity check fails fast at the source rather than letting an INSERT with
    a null agent_id defer the failure to a DB constraint violation (the case of
    `meiva` imported without a bootstrap, e.g. an ad-hoc `python -c`).

    Raises:
        RuntimeError: this process does not own the agent turn loop (a launched
            background script), or has no established identity.
    """
    _assert_native_harness(action)
    refused = self_action_refusal(caller_facts(_bound(), harness=_harness_name()), action)
    if refused is not None:
        raise RuntimeError(refused)
