# meiva:note
# Pure unit tests for `gateway.agents.transfer.transfer_plan` and `TransferBundleStore`.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import pytest

from gateway.agents.transfer.bundle_store import TransferBundleStore
from gateway.agents.transfer.transfer_plan import (
    AgentRowFacts,
    ImpersonationConflictFact,
    MachineFacts,
    PendingLifecycleFact,
    PendingUploadFact,
    TransferPlan,
    TransferPrecheckFacts,
    TransferRefusal,
    compose_transfer_wake_message,
    evaluate_precheck,
    verify_cutover_cas,
)


def _agent(
    *,
    machine: str = "macmini",
    status: str = "idling",
    harness: str = "meiva",
) -> AgentRowFacts:
    return AgentRowFacts(
        agent_id=2697,
        machine=machine,
        status=status,
        harness=harness,
    )


def _machine(
    name: str,
    *,
    roles: tuple[str, ...] = ("agent-runner",),
    paused: bool = False,
    stopped: bool = False,
) -> MachineFacts:
    return MachineFacts(
        name=name,
        gateway_url=f"http://{name}.local:9100",
        roles=roles,
        paused=paused,
        stopped=stopped,
    )


def test_precheck_accepts_idling_and_terminated_native_agents() -> None:
    src = _machine("macmini", roles=("gateway", "agent-runner"))
    dst = _machine("company-mini")

    for status in ("idling", "terminated"):
        decision = evaluate_precheck(
            TransferPrecheckFacts(
                agent=_agent(status=status),
                agent_id=2697,
                to_machine="company-mini",
                source_machine=src,
                target_machine=dst,
            )
        )
        assert isinstance(decision, TransferPlan)
        assert decision.snapshot.status == status
        assert decision.to_machine == "company-mini"


def test_precheck_refuses_running_external_impersonated_and_invalid_targets() -> None:
    src = _machine("macmini")
    dst = _machine("company-mini")

    missing = evaluate_precheck(
        TransferPrecheckFacts(
            agent=None,
            agent_id=2697,
            to_machine="company-mini",
            target_machine=dst,
        )
    )
    assert isinstance(missing, TransferRefusal) and missing.status_code == 404

    running = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(status="running"),
            agent_id=2697,
            to_machine="company-mini",
            source_machine=src,
            target_machine=dst,
        )
    )
    assert isinstance(running, TransferRefusal) and running.status_code == 409
    assert "currently running" in running.detail

    external = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(harness="codex"),
            agent_id=2697,
            to_machine="company-mini",
            source_machine=src,
            target_machine=dst,
        )
    )
    assert isinstance(external, TransferRefusal) and external.status_code == 409
    assert "external harness" in external.detail

    open_imp = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="company-mini",
            impersonation=ImpersonationConflictFact(
                session_id=20, status="active", delta_unapplied=False, handoff_unapplied=True
            ),
            source_machine=src,
            target_machine=dst,
        )
    )
    assert isinstance(open_imp, TransferRefusal) and open_imp.status_code == 409
    assert "session 20" in open_imp.detail and "open impersonation" in open_imp.detail

    unsettled_imp = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="company-mini",
            impersonation=ImpersonationConflictFact(
                session_id=20, status="released", delta_unapplied=False, handoff_unapplied=True
            ),
            source_machine=src,
            target_machine=dst,
        )
    )
    assert isinstance(unsettled_imp, TransferRefusal) and unsettled_imp.status_code == 409
    assert "unsettled impersonation handoff" in unsettled_imp.detail

    pending_lc = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="company-mini",
            pending_lifecycle=PendingLifecycleFact(kind="restart", status="pending"),
            source_machine=src,
            target_machine=dst,
        )
    )
    assert isinstance(pending_lc, TransferRefusal) and pending_lc.status_code == 409

    pending_up = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="company-mini",
            pending_upload=PendingUploadFact(batch_id="b1", state="pending"),
            source_machine=src,
            target_machine=dst,
        )
    )
    assert isinstance(pending_up, TransferRefusal) and pending_up.status_code == 409

    same_machine = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="macmini",
            source_machine=src,
            target_machine=src,
        )
    )
    assert isinstance(same_machine, TransferRefusal) and same_machine.status_code == 409

    paused_target = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="company-mini",
            source_machine=src,
            target_machine=_machine("company-mini", paused=True),
        )
    )
    assert isinstance(paused_target, TransferRefusal) and paused_target.status_code == 409


def test_verify_cutover_cas_detects_concurrent_mutations() -> None:
    plan = evaluate_precheck(
        TransferPrecheckFacts(
            agent=_agent(),
            agent_id=2697,
            to_machine="company-mini",
            source_machine=_machine("macmini"),
            target_machine=_machine("company-mini"),
        )
    )
    assert isinstance(plan, TransferPlan)
    snap = plan.snapshot

    assert verify_cutover_cas(snap, _agent(status="terminated")) is None
    assert verify_cutover_cas(snap, None) is not None
    assert verify_cutover_cas(snap, _agent(status="running")) is not None
    assert verify_cutover_cas(snap, _agent(status="idling")) is not None
    assert verify_cutover_cas(snap, _agent(status="terminated", machine="other")) is not None
    assert verify_cutover_cas(snap, _agent(status="terminated", harness="codex")) is not None


def test_compose_transfer_wake_message_and_bundle_store_cleanup() -> None:
    note = compose_transfer_wake_message(
        from_machine="macmini",
        to_machine="company-mini",
        source="user",
        message="Rebalancing workload",
        killed_shell_sessions=[1, 3],
        closed_pages=["dashboard"],
    )
    assert "Cross-machine transfer from 'macmini' to 'company-mini' initiated by user" in note
    assert "1, 3" in note
    assert "dashboard" in note
    assert "Reason: Rebalancing workload" in note

    store = TransferBundleStore()
    tid = "aa" * 16
    with pytest.raises(RuntimeError), store.stage(tid, b"bundle-payload"):
        assert store.get(tid) == b"bundle-payload"
        raise RuntimeError("boom")
    assert store.get(tid) is None
