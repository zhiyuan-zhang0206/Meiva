# meiva:note
# GET/PUT /api/config integration tests.
#
# Regression: required fields (db_url / redis_url etc. with no default) must not
# let FastAPI hit the PydanticUndefined sentinel during response serialization and 500.
#
# PUT scope routing: the self PUT splits the body by ownership scope — cluster
# keys are written to this gateway's .env, host keys to this machine's .env (the
# single config store); agent-scope fields have writable=False and are rejected by
# the writable gate ("unknown or read-only fields"), same as any other read-only field.
#
# Cross-machine (`?machine=`): GET/PUT POST the host section to a remote
# agent-runner's ops server. The remote runner is a real listener on port 0
# (`runner_ops`) registered in `machines`, so a known host that answers 404 503s
# as unreachable, a failed op 503s, and an unknown name 404s. The local machine is
# served by the in-process ops route the `gateway_local_runner` fixture registers.

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import runtime_config
from base.config.service_read import ConfigAuthority
from base.host import config_validators
from base.host.env.tests.env_files import unit_env_aliases, unit_env_path
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.support.runner_ops.ops_stub import (
    completed,
    failed,
    refused,
    runner_ops,
)


def test_get_config_serializes_required_fields():
    """Settings fields with no default should serialize default_value as null, rather
    than leaking the PydanticUndefined sentinel and causing the entire response to
    500."""
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "fields" in body
    assert "raw_overrides" in body

    by_name = {f["name"]: f for f in body["fields"]}
    # db_url / redis_url have no default on Settings — the core of this regression
    # test. Tests/evals carry no separate DB URL env var; they self-provision a
    # throwaway native Postgres and override db_url at runtime.
    for name in ("db_url", "redis_url"):
        assert name in by_name, f"{name} should be in ConfigView"
        assert by_name[name]["default_value"] is None
        # current_value comes from .env; the test env makes no strong assumption about the specific value, but it should be str
        assert isinstance(by_name[name]["current_value"], str)


def test_redis_url_masked_does_not_leak_cluster_secret() -> None:
    """redis_url carries the runtime ACL password as userinfo, so GET /api/config
    must mask it like db_url — the credential must never appear in the response."""
    from ops.host_config import SENSITIVE_MASK

    with gateway_test_client(
        fields={"redis_url": "redis://:supersecret@10.0.0.1:6379/0"}
    ) as client:
        resp = client.get("/api/config")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["redis_url"]["sensitive"] is True
    assert fields["redis_url"]["current_value"] == SENSITIVE_MASK
    assert "supersecret" not in resp.text


def test_config_fields_expose_scope() -> None:
    """Each config field in GET /api/config carries its ownership scope."""
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["db_url"]["scope"] == "cluster-pinned"
    assert fields["llm_model"]["scope"] == "cluster-default"
    assert fields["machine_name"]["scope"] == "host"
    assert fields["sdk_disable"]["scope"] == "agent"


def test_config_fields_expose_capability() -> None:
    """Each config field in GET /api/config carries its owning capability — the
    top-level config-panel section, independent of scope (a cluster-scoped field
    like db_url is still gateway-owned). llm_model is common: the cluster-wide
    spawn default is read by the gateway (spawn pre-select / default-model) AND
    frozen into agents at spawn — owned by neither capability alone."""
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["db_url"]["capability"] == "gateway"
    assert fields["gateway_port"]["capability"] == "gateway"
    assert fields["llm_model"]["capability"] == "common"
    assert fields["ops_concurrency"]["capability"] == "agent-runner"
    assert fields["timezone"]["capability"] == "common"


def test_get_config_exposes_target_machine_capabilities(
    *, machine_identity: MachineIdentityReader
) -> None:
    """GET self carries the gateway's own capability set (machine_role()) so the
    panel can pick which capability sections a remote view renders. Only valid
    capability tokens; the self view is a gateway, so it carries at least that."""

    with gateway_test_client(machine=machine_identity) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    caps = resp.json()["machine_capabilities"]
    assert isinstance(caps, list)
    # The projection carries the full capability set incl. observability-station
    # (WP1/WP2; a station-capable gateway projects its station token to the
    # panel — task #1945 WP3 verification).
    assert set(caps) <= {"gateway", "agent-runner", "observability-station"}  # pyright: ignore[reportUnknownArgumentType]
    assert set(caps) == set(machine_identity.role())  # pyright: ignore[reportUnknownArgumentType]


def test_enum_field_exposes_type_and_choices() -> None:
    """A Literal[...] config field serializes as field_type "enum" with its
    allowed values in `choices`; a non-enum field carries choices null."""
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    enum_field = fields["agent_reply_reminder_cadence"]
    assert enum_field["field_type"] == "enum"
    assert enum_field["choices"] == ["once_per_compaction", "every_time"]
    # A scalar field carries no choices.
    assert fields["llm_model"]["choices"] is None


@pytest.fixture
def stub_browser_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the browser_enabled read-time capability to a deterministic verdict:
    the aggregate browser_capable() gate reports capable -> can_enable True.

    Patches the predicate as bound in `config_validators`: no real display / Chrome / npx.
    Retained: the display probe reads the ambient environment and the macOS Chrome path
    is read from disk, so no `Host` value makes a "capable" host on every machine (needs
    base: display and Chrome path as `Host` facts).
    """
    monkeypatch.setattr(config_validators, "browser_capable", lambda _chrome, **_kwargs: True)  # pyright: ignore[reportUnknownArgumentType]


def test_get_self_host_fields_carry_capability_hint(stub_browser_capability: None) -> None:
    """GET self: host fields carry a can_enable/reason hint where one exists
    (browser_enabled), cluster fields carry null for both."""
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    fields = {f["name"]: f for f in resp.json()["fields"]}

    # browser_enabled is host-scope with a read-time capability gate; the stub
    # makes the verdict deterministic.
    be = fields["browser_enabled"]
    assert be["scope"] == "host"
    assert be["can_enable"] is True
    assert be["reason"] is None

    # A cluster field has no per-host capability hint.
    assert fields["llm_model"]["can_enable"] is None
    assert fields["llm_model"]["reason"] is None
    # A host field with no read-time gate (free-text / numbers) also has null.
    assert fields["ops_concurrency"]["can_enable"] is None


# meiva:note
# GET self with no display: browser_enabled.can_enable is False + a reason. The display probe
# reads the ambient environment, so no host value makes a headless machine on every platform;
# the probe is replaced here, once.
def test_get_self_browser_disabled_when_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_display(*, host: Host) -> bool:
        return False

    monkeypatch.setattr(config_validators, "display_available", no_display)
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    be = {f["name"]: f for f in resp.json()["fields"]}["browser_enabled"]
    assert be["can_enable"] is False
    assert be["reason"] == "no display detected"


REMOTE = "remote-host"


def test_get_remote_machine_composes_host_from_dispatch(
    db_conn: psycopg.Connection, *, config_authority: ConfigAuthority
) -> None:
    """GET ?machine=<remote>: host fields reflect the remote config_read result,
    cluster fields reflect local metadata, raw_overrides == the remote host
    overrides."""
    # The GET composer iterates every host-scope meta and indexes the result, so
    # the canned host_fields must answer for all of them — fill the explicit ones
    # plus a neutral stub for the rest.
    explicit_host: dict[str, Any] = {
        "browser_enabled": {
            "value": True,
            "overridden": True,
            "remote_writable": True,
            "can_enable": True,
            "reason": None,
        },
        "ops_concurrency": {
            "value": 16,
            "overridden": True,
            "remote_writable": True,
            "can_enable": None,
            "reason": None,
        },
    }
    canned: dict[str, Any] = {
        "machine": REMOTE,
        "host_fields": _full_host_fields_for_remote(
            explicit_host, config_authority=config_authority
        ),
        "raw_overrides": {"browser_enabled": True, "ops_concurrency": 16},
    }

    with (
        runner_ops(db_conn, REMOTE, {"config_read": completed(canned)}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/config?machine={REMOTE}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fields = {f["name"]: f for f in body["fields"]}

    # host fields reflect the canned remote values
    assert fields["browser_enabled"]["current_value"] is True
    assert fields["browser_enabled"]["can_enable"] is True
    assert fields["ops_concurrency"]["current_value"] == 16
    # cluster fields reflect local metadata (the gateway's own)
    assert fields["llm_model"]["scope"] == "cluster-default"
    assert fields["llm_model"]["can_enable"] is None
    # raw_overrides == the remote host overrides (not the local merged set)
    assert body["raw_overrides"] == {"browser_enabled": True, "ops_concurrency": 16}


def _full_host_fields_for_remote(
    overrides: dict[str, Any], *, config_authority: ConfigAuthority
) -> dict[str, Any]:
    """Build a full host_fields dict for every host-scope meta, taking explicit
    overrides where given and a neutral stub otherwise — so the GET composer can
    index every host field without a real remote runner."""
    from base.packages.plugin_config_admin import panel_metadata

    out: dict[str, Any] = {}
    for meta in panel_metadata(authority=config_authority):
        if meta.scope != "host":
            continue
        if meta.name in overrides:
            out[meta.name] = overrides[meta.name]
        else:
            out[meta.name] = {
                "value": meta.default_value,
                "overridden": False,
                "remote_writable": meta.remote_writable,
                "can_enable": None,
                "reason": None,
            }
    return out


def test_get_unknown_machine_404() -> None:
    """GET ?machine=<unknown> (no row in machines) -> 404."""
    with gateway_test_client() as client:
        resp = client.get("/api/config?machine=ghost-host")
    assert resp.status_code == 404, resp.text
    assert "unknown machine" in resp.json()["detail"]


def test_get_offline_machine_503(db_conn: psycopg.Connection) -> None:
    """GET ?machine=<known-but-offline>: ops server unreachable -> 503."""
    with (
        runner_ops(db_conn, REMOTE, {"config_read": refused(404, "no ack")}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/config?machine={REMOTE}")
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_get_remote_failed_503(db_conn: psycopg.Connection) -> None:
    """GET ?machine=<known>: the remote op fails (row marked failed) -> 503."""
    with (
        runner_ops(db_conn, REMOTE, {"config_read": failed({"error": "boom"})}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/config?machine={REMOTE}")
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


# meiva:note
# The LOCAL machine's ops server unreachable -> 503 like any other machine.
# The router has NO in-process fallback — a silent local read would mask the
# outage (the old behavior). The listener registered here replaces the in-process
# ops route the `gateway_local_runner` fixture serves for the local machine.
def test_get_local_unreachable_503_no_in_process_fallback(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    with (
        runner_ops(
            db_conn,
            machine_identity.name(),
            {"config_read": refused(404, "no ack")},
            roles=("gateway", "agent-runner"),
        ),
        gateway_test_client(machine=machine_identity) as client,
    ):
        resp = client.get("/api/config")  # machine absent -> the local machine
    assert resp.status_code == 503, resp.text
    assert "unreachable" in resp.json()["detail"]


# ── PUT ──


@pytest.fixture
def clean_overrides(unit_home: Path) -> Path:
    """Redirect this gateway's .env (the single config store) to a fresh tmp file,
    so a PUT in these tests writes there instead of the real unit .env. The PUT no
    longer mutates the in-memory Settings (strict persist-and-signal), so there is
    nothing to save/restore."""
    return unit_home


def test_put_self_returns_write_result_and_routes_scopes(clean_overrides: Path) -> None:
    """PUT self with a good cluster + host mix -> 200 + ConfigWriteResult,
    applied True, both stores written, restart_required is the union."""
    with gateway_test_client() as client:
        resp = client.put(
            "/api/config",
            json={"llm_model": "put-cluster", "ops_concurrency": 4},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    # ops_concurrency (host) is the per-field result; cluster fields don't
    # appear (they can't fail a capability check).
    assert body["results"]["ops_concurrency"]["ok"] is True
    # restart_required = union of cluster llm_model ("agent") + host
    # ops_concurrency ("ops"), sorted.
    assert body["restart_required"] == ["agent", "ops"]

    # Both landed in this gateway's .env (the single store): cluster llm_model +
    # host ops_concurrency.
    assert runtime_config.env_set_field_names(unit_env_path()) == {"llm_model", "ops_concurrency"}
    aliases = unit_env_aliases()
    assert aliases["MEIVA_MODEL"] == "put-cluster"
    assert aliases["MEIVA_OPS_CONCURRENCY"] == "4"


def test_put_self_capability_failure_writes_nothing(clean_overrides: Path) -> None:
    """PUT self where a host field fails the capability validator -> applied
    False, results[field].ok False with a reason, and NOTHING written locally."""
    with gateway_test_client() as client:
        # ops_concurrency=65 is out of the [1, 64] range the host validator
        # enforces -> the write is rejected atomically.
        resp = client.put("/api/config", json={"ops_concurrency": 65})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["results"]["ops_concurrency"]["ok"] is False
    assert body["results"]["ops_concurrency"]["reason"]
    # atomic: nothing landed in .env
    assert unit_env_aliases() == {}


def test_put_rejects_agent_scope_field(clean_overrides: Path) -> None:
    """An agent-scope field is writable=False, so the writable gate rejects it.

    sdk_disable has scope="agent" and writable=False; a PUT carrying it is
    rejected as "unknown or read-only" — same path as any other read-only field.
    Nothing is written to either store.
    """
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"sdk_disable": ["monitor"]})
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    # nothing written to .env
    assert unit_env_aliases() == {}


def test_put_rejects_unknown_field(clean_overrides: Path) -> None:
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"not_a_field": 1})
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]


def test_put_accepts_valid_enum_value(clean_overrides: Path) -> None:
    """A cluster enum field accepts a value inside its choices and lands in .env."""
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"agent_reply_reminder_cadence": "every_time"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert unit_env_aliases()["MEIVA_AGENT_REPLY_REMINDER_CADENCE"] == "every_time"


def test_put_rejects_invalid_enum_value(clean_overrides: Path) -> None:
    """A value outside the enum's choices 400s atomically (nothing written), so an
    unknown enum can never reach .env and blow up Settings at the next restart."""
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"agent_reply_reminder_cadence": "sometimes"})
    assert resp.status_code == 400, resp.text
    assert "invalid enum value" in resp.json()["detail"]
    assert unit_env_aliases() == {}


def test_get_self_sensitive_raw_override_is_sentinel(clean_overrides: Path) -> None:
    """A sensitive cluster field set in .env appears in raw_overrides as the
    unchanged-sentinel, never its cleartext (no secret on the wire)."""
    from base.config import CONFIG_UNCHANGED_SENTINEL

    runtime_config.write_fields({"deepseek_api_key": "sk-REAL-SECRET"}, set())
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    assert resp.json()["raw_overrides"]["deepseek_api_key"] == CONFIG_UNCHANGED_SENTINEL
    assert "sk-REAL-SECRET" not in resp.text


def test_put_sentinel_preserves_secret(clean_overrides: Path) -> None:
    """Round-tripping the sentinel for an untouched secret keeps its .env value;
    a real edit to another field still lands."""
    from base.config import CONFIG_UNCHANGED_SENTINEL

    runtime_config.write_fields({"deepseek_api_key": "sk-REAL-SECRET"}, set())
    with gateway_test_client() as client:
        resp = client.put(
            "/api/config",
            json={"deepseek_api_key": CONFIG_UNCHANGED_SENTINEL, "llm_model": "m2"},
        )
    assert resp.status_code == 200, resp.text
    aliases = unit_env_aliases()
    assert aliases["DEEPSEEK_API_KEY"] == "sk-REAL-SECRET"  # untouched
    assert aliases["MEIVA_MODEL"] == "m2"


def test_put_absent_key_preserves_secret(clean_overrides: Path) -> None:
    """Reducer semantics: a PUT that does NOT mention a secret leaves it in place.

    This is the exact incident that once wiped a cluster's keys — a single-field
    PUT (here just llm_model) must not drop the DEEPSEEK/OPENAI keys it never
    named. Under the old full-replace, any managed key absent from the body was
    unset; under merge semantics an absent key is untouched."""
    runtime_config.write_fields(
        {"deepseek_api_key": "sk-REAL-SECRET", "openai_api_key": "sk-OPENAI"}, set()
    )
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"llm_model": "m2"})
    assert resp.status_code == 200, resp.text
    aliases = unit_env_aliases()
    assert aliases["DEEPSEEK_API_KEY"] == "sk-REAL-SECRET"  # untouched (absent from body)
    assert aliases["OPENAI_API_KEY"] == "sk-OPENAI"  # untouched (absent from body)
    assert aliases["MEIVA_MODEL"] == "m2"


def test_put_replaces_one_secret_leaves_others(clean_overrides: Path) -> None:
    """The API-key replace flow: the frontend PUTs a real new value for the ONE
    secret being replaced and the unchanged-sentinel for every other set secret
    (that is what raw_overrides carries). The named secret is rewritten; the others
    keep their .env value untouched — changing one key never disturbs another.

    Locks the isolation guarantee behind the Control page's write-only API-key
    replace: a new DEEPSEEK value lands while OPENAI (sentinel) and a non-secret
    field are preserved."""
    from base.config import CONFIG_UNCHANGED_SENTINEL

    runtime_config.write_fields(
        {
            "deepseek_api_key": "sk-OLD-DEEPSEEK",
            "openai_api_key": "sk-OPENAI",
            "llm_model": "m1",
        },
        set(),
    )
    with gateway_test_client() as client:
        resp = client.put(
            "/api/config",
            json={
                "deepseek_api_key": "sk-NEW-DEEPSEEK",  # the replaced key
                "openai_api_key": CONFIG_UNCHANGED_SENTINEL,  # untouched secret
                "llm_model": "m1",  # untouched non-secret (idempotent)
            },
        )
    assert resp.status_code == 200, resp.text
    aliases = unit_env_aliases()
    assert aliases["DEEPSEEK_API_KEY"] == "sk-NEW-DEEPSEEK"  # rewritten
    assert aliases["OPENAI_API_KEY"] == "sk-OPENAI"  # untouched (sentinel)
    assert aliases["MEIVA_MODEL"] == "m1"


def test_put_null_unsets_cluster_field(clean_overrides: Path) -> None:
    """Reducer semantics: a cluster field mapped to null is unset (reverted to its
    default); other set fields are untouched. Deletion is explicit, never by
    omission."""
    runtime_config.write_fields({"llm_model": "m1", "deepseek_api_key": "sk-X"}, set())
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"llm_model": None})
    assert resp.status_code == 200, resp.text
    aliases = unit_env_aliases()
    assert "MEIVA_MODEL" not in aliases  # explicitly unset
    assert aliases["DEEPSEEK_API_KEY"] == "sk-X"  # untouched


def test_put_host_failure_leaves_cluster_unwritten(clean_overrides: Path) -> None:
    """Atomic across scopes: a rejected host field means the cluster .env is not
    touched (host is written/validated first)."""
    with gateway_test_client() as client:
        resp = client.put(
            "/api/config",
            json={"llm_model": "should-not-persist", "ops_concurrency": 65},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False
    assert "MEIVA_MODEL" not in unit_env_aliases()


def test_put_rejects_bad_scalar(
    clean_overrides: Path, *, config_authority: ConfigAuthority
) -> None:
    """A non-numeric value for a cluster int/float field 400s before any write,
    rather than landing in .env and crashing Settings at the next restart."""
    from base.packages.plugin_config_admin import panel_metadata

    scalar = next(
        m
        for m in panel_metadata(authority=config_authority)
        if m.scope != "host" and m.writable and m.field_type in ("int", "float")
    )
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={scalar.name: "not-a-number"})
    assert resp.status_code == 400, resp.text
    assert scalar.name in resp.json()["detail"]
    assert unit_env_aliases() == {}


def test_put_rejects_bool_for_int_field(
    clean_overrides: Path, *, config_authority: ConfigAuthority
) -> None:
    """A JSON bool for an int field is rejected (it would otherwise pass int() and
    persist as 'true', breaking Settings at the next restart)."""
    from base.packages.plugin_config_admin import panel_metadata

    scalar = next(
        m
        for m in panel_metadata(authority=config_authority)
        if m.scope != "host" and m.writable and m.field_type == "int"
    )
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={scalar.name: True})
    assert resp.status_code == 400, resp.text
    assert unit_env_aliases() == {}


def test_put_nonsensitive_field_can_hold_literal_sentinel(clean_overrides: Path) -> None:
    """The sentinel-skip is gated on `sensitive`, so a non-sensitive field can be
    set to the literal sentinel string (no silent drop)."""
    from base.config import CONFIG_UNCHANGED_SENTINEL

    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"llm_model": CONFIG_UNCHANGED_SENTINEL})
    assert resp.status_code == 200, resp.text
    assert unit_env_aliases()["MEIVA_MODEL"] == CONFIG_UNCHANGED_SENTINEL


def test_put_self_edits_writable_host_capability_field(clean_overrides: Path) -> None:
    """A writable host capability field (cross_machine_transfer_backend, not
    remote_writable) is editable on the self/Cluster view — the local write honors
    `writable`."""
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"cross_machine_transfer_backend": "none"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert unit_env_aliases()["MEIVA_CROSS_MACHINE_TRANSFER_BACKEND"] == "none"


def test_put_self_host_field_skips_empty_cluster_audit(clean_overrides: Path) -> None:
    """A host-only self PUT must not append an empty gateway cluster record."""
    with gateway_test_client() as client:
        response = client.put("/api/config", json={"cross_machine_transfer_backend": "none"})

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    audit = clean_overrides / ".env.audit.jsonl"
    sites = (
        [json.loads(line)["site"] for line in audit.read_text().splitlines()]
        if audit.exists()
        else []
    )
    assert "gateway_config_put" not in sites


def test_put_self_rejects_readonly_identity_field(clean_overrides: Path) -> None:
    """An identity / connection field (machine_name) is read-only now — rejected by
    the writable gate, same as any other read-only field; nothing written."""
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"machine_name": "renamed"})
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    assert "MEIVA_MACHINE_NAME" not in unit_env_aliases()


def test_get_self_raw_overrides_exposes_writable_host_field_not_readonly(
    clean_overrides: Path,
) -> None:
    """Self GET raw_overrides includes a writable host capability field (editable on
    the host's own panel) but excludes a now-read-only identity field, even when both
    are set in .env."""
    runtime_config.write_fields(
        {"cross_machine_transfer_backend": "none", "machine_name": "m"}, set()
    )
    with gateway_test_client() as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    raw = resp.json()["raw_overrides"]
    assert raw.get("cross_machine_transfer_backend") == "none"  # writable -> self-editable
    assert "machine_name" not in raw  # writable=False -> read-only, not in the edit set


def test_put_explicit_self_edits_remote_writable_host_toggle(
    clean_overrides: Path, *, machine_identity: MachineIdentityReader
) -> None:
    """On a dual-role box, `?machine=<self>` is a host view (remote_writable gate),
    NOT the Cluster view — so a gateway-capability host toggle that is
    writable=False + remote_writable=True (heartbeat_enabled) is editable there,
    while the Cluster view (machine omitted) rejects it as read-only. Regression:
    the two used to collapse because both resolve target to machine_name(), leaving
    heartbeat_enabled / task_maintenance_enabled uneditable on a single box."""
    with gateway_test_client(machine=machine_identity) as client:
        # Cluster view (machine omitted): writable=False -> rejected read-only.
        cluster_resp = client.put("/api/config", json={"heartbeat_enabled": False})
        # Explicit self host view: remote_writable=True -> accepted + written.
        host_resp = client.put(
            f"/api/config?machine={machine_identity.name()}", json={"heartbeat_enabled": False}
        )
    assert cluster_resp.status_code == 400
    assert "unknown or read-only" in cluster_resp.json()["detail"]
    assert host_resp.status_code == 200, host_resp.text
    assert host_resp.json()["applied"] is True
    assert unit_env_aliases()["MEIVA_HEARTBEAT_ENABLED"] == "false"


# ── PUT remote ──


def test_plugin_config_owner_metadata_and_mixed_patch_rejection(clean_overrides: Path) -> None:
    """A mixed request cannot partly commit Core env and a Fleet image."""
    from base.packages.plugins.config_registration import disk_image_path

    unit_env_path().touch()
    before_env = unit_env_path().read_bytes()
    image = disk_image_path("meiva_fleet")
    before_image = image.read_bytes() if image.exists() else None
    with gateway_test_client() as client:
        fields = {field["name"]: field for field in client.get("/api/config").json()["fields"]}
        assert fields["task_escalate_n"]["owner"] == "meiva_fleet"
        assert fields["llm_model"]["owner"] is None
        response = client.put("/api/config", json={"task_escalate_n": 7, "llm_model": "unknown"})
    assert response.status_code == 400
    assert "mixes owners" in response.json()["detail"]
    assert unit_env_path().read_bytes() == before_env
    assert (image.read_bytes() if image.exists() else None) == before_image


def test_fleet_cluster_write_goes_only_to_owned_image(clean_overrides: Path) -> None:
    from base.packages.plugins.config_registration import disk_image_path, read_config_image
    from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

    unit_env_path().touch()
    before_env = unit_env_path().read_bytes()
    with gateway_test_client() as client:
        response = client.put(
            "/api/config", json={"task_escalate_n": 11, "reduce_context_switch": False}
        )
        invalid = client.put("/api/config", json={"task_escalate_n": "wrong"})
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert response.json()["restart_required"] == ["agent", "all"]
    assert invalid.status_code == 400
    saved = read_config_image(FleetConfig, disk_image_path("meiva_fleet"))
    assert saved.task_escalate_n == 11 and saved.reduce_context_switch is False
    assert unit_env_path().read_bytes() == before_env


def test_remote_host_fleet_toggle_respects_owned_image_and_policy(
    *, config_authority: ConfigAuthority, machine_identity: MachineIdentityReader
) -> None:
    from base.packages.plugins.config_registration import disk_image_path, read_config_image
    from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
    from ops.host_config import config_write_op

    rejected = config_write_op(
        {"task_maintenance_enabled": False},
        local=True,
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert rejected.applied is False
    accepted = config_write_op(
        {"task_maintenance_enabled": False},
        local=False,
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert accepted.applied is True
    assert (
        read_config_image(FleetConfig, disk_image_path("meiva_fleet")).task_maintenance_enabled
        is False
    )
    mixed = config_write_op(
        {"task_maintenance_enabled": True, "heartbeat_enabled": False},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert mixed.applied is False
    assert "mixes owners" in str(mixed.results["task_maintenance_enabled"].reason)
    assert (
        read_config_image(FleetConfig, disk_image_path("meiva_fleet")).task_maintenance_enabled
        is False
    )
