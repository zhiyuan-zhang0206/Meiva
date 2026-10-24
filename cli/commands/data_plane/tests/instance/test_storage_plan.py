# meiva:note
# What a gateway's data-plane steps do, asserted as the plans the pure functions return.
#
# The ordering facts that used to be a recorded list of spy calls are one assertion each: an
# ordinary start re-grants and checks before the pooler, a birth mints generation zero and
# activates after the pooler proof, a release readiness performs no schema or grant write.

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands.data_plane.instance.storage_plan import (
    ActivateGeneration,
    AdmitGeneration,
    AdoptLogin,
    BirthGeneration,
    CompletionFacts,
    EnsureExtension,
    EnsurePooler,
    InAdminSession,
    MarkProvisioned,
    PrepareVectors,
    ProbeConsumerDatabases,
    ProbeRedis,
    ProbeRemotePlane,
    ProveLogins,
    RecordedPorts,
    Refuse,
    RefuseLegacy,
    ReportAdmitted,
    StartLocalStorage,
    admit_gateway_storage,
    legacy_home_refusal,
    plan_completion,
    require_storage_credentials,
)

_HOME = Path("/home/meiva")
_PORTS = RecordedPorts(postgres=23011, redis=23012)


def _facts(
    *,
    remote: bool = False,
    refresh_schema: bool = True,
    birth: bool = False,
    ledger_present: bool = True,
) -> CompletionFacts:
    return CompletionFacts(
        home=_HOME,
        remote=remote,
        refresh_schema=refresh_schema,
        birth=birth,
        ledger_present=ledger_present,
    )


def test_a_home_without_a_record_cannot_bring_up_a_data_plane() -> None:
    admission = admit_gateway_storage(home=_HOME, ports=None, remote=False, legacy_home=False)
    assert isinstance(admission, Refuse)
    assert "no registry record for home /home/meiva" in admission.message
    assert "meiva init" in admission.message


def test_the_missing_record_is_refused_before_a_remote_plane_is_considered() -> None:
    admission = admit_gateway_storage(home=_HOME, ports=None, remote=True, legacy_home=False)
    assert isinstance(admission, Refuse)


def test_a_remote_managed_plane_is_probed_not_started() -> None:
    admission = admit_gateway_storage(home=_HOME, ports=_PORTS, remote=True, legacy_home=False)
    assert admission == ProbeRemotePlane()


def test_a_legacy_home_is_refused_before_any_native_effect() -> None:
    admission = admit_gateway_storage(home=_HOME, ports=_PORTS, remote=False, legacy_home=True)
    assert admission == Refuse(f"  ✗ {legacy_home_refusal(_HOME)}")
    assert "no conversion exists" in legacy_home_refusal(_HOME)


def test_a_registered_local_home_starts_its_own_storage_on_the_recorded_ports() -> None:
    admission = admit_gateway_storage(home=_HOME, ports=_PORTS, remote=False, legacy_home=False)
    assert admission == StartLocalStorage(_PORTS)


@pytest.mark.parametrize(("admin", "runtime"), [("", ""), ("", "runtime"), ("admin", "")])
def test_missing_redis_credentials_refuse_with_no_conversion(admin: str, runtime: str) -> None:
    with pytest.raises(ValueError, match="no conversion exists"):
        require_storage_credentials(admin, runtime)


def test_both_redis_credentials_pass() -> None:
    require_storage_credentials("admin", "runtime")


def test_an_ordinary_start_regrants_and_checks_before_the_pooler() -> None:
    assert plan_completion(_facts()) == (
        EnsureExtension(),
        PrepareVectors(),
        InAdminSession(
            (AdmitGeneration(refresh=True), EnsurePooler(), ProveLogins(), ActivateGeneration())
        ),
        AdoptLogin(),
        ReportAdmitted(),
        ProbeRedis(),
        MarkProvisioned(),
    )


def test_a_birth_mints_generation_zero_and_activates_after_the_pooler_proof() -> None:
    plan = plan_completion(_facts(birth=True, ledger_present=False))
    assert plan == (
        EnsureExtension(),
        PrepareVectors(),
        InAdminSession((BirthGeneration(), EnsurePooler(), ProveLogins(), ActivateGeneration())),
        AdoptLogin(),
        ReportAdmitted(),
        ProbeRedis(),
        MarkProvisioned(),
    )


def test_release_readiness_performs_no_schema_or_grant_write() -> None:
    plan = plan_completion(_facts(refresh_schema=False))
    assert EnsureExtension() not in plan and PrepareVectors() not in plan
    assert plan[0] == InAdminSession(
        (AdmitGeneration(refresh=False), EnsurePooler(), ProveLogins(), ActivateGeneration())
    )


def test_the_birth_is_journaled_only_after_every_check() -> None:
    plan = plan_completion(_facts(birth=True, ledger_present=False))
    assert plan[-2:] == (ProbeRedis(), MarkProvisioned())
    assert plan.index(AdoptLogin()) < plan.index(ProbeRedis())


def test_a_home_without_a_ledger_that_is_not_being_born_refuses_after_the_schema_prep() -> None:
    plan = plan_completion(_facts(ledger_present=False))
    assert plan == (
        EnsureExtension(),
        PrepareVectors(),
        RefuseLegacy(legacy_home_refusal(_HOME)),
    )


def test_a_legacy_refusal_on_a_release_readiness_touches_nothing_first() -> None:
    plan = plan_completion(_facts(ledger_present=False, refresh_schema=False))
    assert plan == (RefuseLegacy(legacy_home_refusal(_HOME)),)


def test_a_remote_plane_prepares_vectors_and_verifies_its_consumers() -> None:
    assert plan_completion(_facts(remote=True)) == (
        PrepareVectors(),
        ProbeConsumerDatabases(),
        ProbeRedis(),
        MarkProvisioned(),
    )


def test_a_remote_plane_on_a_release_readiness_only_verifies() -> None:
    assert plan_completion(_facts(remote=True, refresh_schema=False)) == (
        ProbeConsumerDatabases(),
        ProbeRedis(),
        MarkProvisioned(),
    )


def test_a_remote_plane_never_enters_a_local_administrator_session() -> None:
    plan = plan_completion(_facts(remote=True, birth=True, ledger_present=False))
    assert not [step for step in plan if isinstance(step, InAdminSession)]
