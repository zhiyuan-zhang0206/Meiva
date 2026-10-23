# meiva:link ops/docs/ops-module.md


from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import runtime_config
from base.config.service_read import ConfigAuthority
from base.host.env.tests.env_files import unit_env_aliases, unit_env_path
from base.host.proc import run_bounded
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugin_config_admin import panel_metadata, panel_overrides
from ops.host_config import config_audit_read_op, config_read_op, config_write_op
from ops.rpc_schemas import ConfigReadResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _host_field_names(*, config_authority: ConfigAuthority) -> list[str]:
    return [m.name for m in panel_metadata(authority=config_authority) if m.scope == "host"]


_SENSITIVE_HOST_FIELD = "test_host_secret"


@pytest.fixture
def sensitive_host_field(unit_home: Path) -> str:
    # meiva:note
    # Core has no sensitive host field; a real plugin declaration exercises both
    # panel metadata and persisted image overrides through their owning loaders.
    plugin = unit_home / "plugins" / "test_host_config"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text("")
    (plugin / "default_config.py").write_text(
        "from pydantic import BaseModel, Field\n"
        "from base.packages.plugins.extensions import PluginContributions\n"
        "class Config(BaseModel):\n"
        "    test_host_secret: str = Field(default='default-secret', "
        "json_schema_extra={'scope': 'host', 'sensitive': True, 'remote_writable': True})\n"
        "def contribute():\n"
        "    return PluginContributions(config=Config)\n"
    )
    image = unit_home / "configs" / "test_host_config" / "config.json"
    image.parent.mkdir(parents=True)
    image.write_text(json.dumps({_SENSITIVE_HOST_FIELD: "stored-secret"}))
    return _SENSITIVE_HOST_FIELD


# ---------------------------------------------------------------------------
# config_read_op
# ---------------------------------------------------------------------------


def test_config_read_op_returns_all_host_fields(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """config_read_op returns one entry per host-scope field."""
    machine_identity.set(name="test-machine")
    result = config_read_op(
        authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    host_names = set(_host_field_names(config_authority=config_authority))
    assert host_names == set(result.host_fields.keys())


def test_config_read_op_sensitive_field_masked(
    unit_home: Path,
    sensitive_host_field: str,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Sensitive field values are replaced with the mask string."""
    machine_identity.set(name="test-machine")
    name = sensitive_host_field
    meta = next(m for m in panel_metadata(authority=config_authority) if m.name == name)
    assert meta.current_value == "stored-secret"
    result = config_read_op(
        authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    entry = result.host_fields[name]
    assert entry.value == "••••••••", f"{name} should be masked but got {entry.value!r}"


def test_config_read_op_overridden_flag_reflects_local_file(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """'overridden' is True for a field present in the local runtime_config.json."""
    machine_identity.set(name="test-machine")
    # Set ops_concurrency in this machine's .env
    runtime_config.write_fields({"ops_concurrency": 7}, set())
    result = config_read_op(
        authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    assert result.host_fields["ops_concurrency"].overridden is True
    # A field not in the local file should not be marked overridden
    not_overridden = [
        n for n in _host_field_names(config_authority=config_authority) if n != "ops_concurrency"
    ]
    for name in not_overridden:
        assert result.host_fields[name].overridden is False, f"{name} should not be overridden"


@pytest.mark.parametrize("display", [True, False])
def test_config_read_op_reports_real_browser_capability(unit_home: Path, display: bool) -> None:
    chrome = unit_home / "test-chrome"
    chrome.touch()
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"DISPLAY", "WAYLAND_DISPLAY", "MEIVA_PROCESS_PROFILE"}
    }
    env["MEIVA_CHROME_BINARY"] = str(chrome)
    if display:
        env["DISPLAY"] = ":test"
    child = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_host_capability_child.py")),
            "config",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    result = ConfigReadResult.model_validate_json(child.stdout)
    entry = result.host_fields["browser_enabled"]
    assert entry.can_enable is display
    assert entry.reason == (None if display else "no display detected")


def test_config_read_op_non_gated_field_has_null_can_enable(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Fields with no static read-time gate get can_enable=None, reason=None."""
    machine_identity.set(name="test-machine")
    result = config_read_op(
        authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    # machine_description has no entry in VALIDATORS and no read_time_capability
    entry = result.host_fields["machine_description"]
    assert entry.can_enable is None
    assert entry.reason is None


def test_config_read_op_raw_overrides_omits_sensitive(
    unit_home: Path,
    sensitive_host_field: str,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """raw_overrides omits keys that are sensitive host fields."""
    machine_identity.set(name="test-machine")
    name = sensitive_host_field
    runtime_config.write_fields({"ops_concurrency": 3}, set())
    assert name in panel_overrides(authority=config_authority)
    result = config_read_op(
        authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    raw = result.raw_overrides
    assert name not in raw, f"sensitive field {name} must be omitted from raw_overrides"
    assert raw.get("ops_concurrency") == 3


def test_config_read_op_machine_name_in_result(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Result includes the explicitly supplied local machine name."""
    machine_identity.set(name="my-host")
    result = config_read_op(
        authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    assert result.machine == "my-host"


# ---------------------------------------------------------------------------
# config_write_op
# ---------------------------------------------------------------------------


def test_config_write_op_rejects_unknown_field(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """An unknown field name returns ok=False with reason='unknown field'."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"totally_unknown_field": 42},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    assert result.results["totally_unknown_field"].ok is False
    assert "unknown field" in (result.results["totally_unknown_field"].reason or "")


def test_config_write_op_rejects_non_host_field(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A cluster-scope field is rejected with reason='not a host-scope field'."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"llm_model": "something"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    assert result.results["llm_model"].ok is False
    assert "host-scope" in (result.results["llm_model"].reason or "")


def test_config_write_op_rejects_non_remote_writable(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A host field with remote_writable=False is rejected."""
    machine_identity.set(name="test-machine")
    # Find a host-scope field that is NOT remote_writable
    non_writable = [
        m.name
        for m in panel_metadata(authority=config_authority)
        if m.scope == "host" and not m.remote_writable
    ]
    if not non_writable:
        raise AssertionError(
            "all host fields are remote_writable — the non-remote-writable "
            "rejection path is no longer exercised (config changed)"
        )
    result = config_write_op(
        {non_writable[0]: "any-value"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    assert result.results[non_writable[0]].ok is False
    assert "not remotely editable" in (result.results[non_writable[0]].reason or "")


def test_config_write_op_local_accepts_writable_host_field(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A writable-but-not-remote_writable host field (a capability field) is editable
    on a local (self) write — `writable` means a human may edit it on its own host."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"cross_machine_transfer_backend": "none"},
        local=True,
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is True
    assert runtime_config.env_set_field_names(unit_env_path()) == {"cross_machine_transfer_backend"}


def test_config_write_op_remote_rejects_writable_only_host_field(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """The same field is rejected on a remote write (local defaults False) — the
    gateway may not edit a remote host's non-remote_writable field."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"cross_machine_transfer_backend": "none"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    assert "not remotely editable" in (
        result.results["cross_machine_transfer_backend"].reason or ""
    )


def test_config_write_op_rejects_failing_validator(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A value that fails the field validator is rejected."""
    machine_identity.set(name="test-machine")
    # ops_concurrency must be int in [1, 64] — 0 should fail
    result = config_write_op(
        {"ops_concurrency": 0},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    assert result.results["ops_concurrency"].ok is False
    assert result.results["ops_concurrency"].reason is not None


def test_config_write_op_rejects_unknown_transfer_backend(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """The backend selector is a closed set: a typo (or a stale value from a
    removed option) is rejected at write time, never persisted to .env."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"cross_machine_transfer_backend": "scp"},
        local=True,
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    assert result.results["cross_machine_transfer_backend"].ok is False
    assert "must be one of" in (result.results["cross_machine_transfer_backend"].reason or "")


def test_config_write_op_atomic_one_bad_writes_nothing(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Atomic: one bad field + one good field -> nothing written, applied=False."""
    machine_identity.set(name="test-machine")
    # One body mixing a failing field (ops_concurrency out of range) with a
    # passing one (machine_description free text) must write neither — atomicity.
    result = config_write_op(
        {"ops_concurrency": 0, "machine_description": "ok"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is False
    # Nothing should be written to .env
    assert unit_env_aliases() == {}


def test_config_write_op_all_good_writes_file(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A fully-valid body writes the local file and returns applied=True."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"ops_concurrency": 4, "machine_description": "test"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is True
    assert runtime_config.env_set_field_names(unit_env_path()) == {
        "ops_concurrency",
        "machine_description",
    }
    assert unit_env_aliases()["MEIVA_OPS_CONCURRENCY"] == "4"


def test_config_write_op_none_unsets_field(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Reducer semantics: a field mapped to None is unset (reverts to default)."""
    machine_identity.set(name="test-machine")
    config_write_op(
        {"ops_concurrency": 4, "machine_description": "hi"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert runtime_config.env_set_field_names(unit_env_path()) == {
        "ops_concurrency",
        "machine_description",
    }
    # Explicit None unsets machine_description; ops_concurrency is untouched.
    result = config_write_op(
        {"machine_description": None},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is True
    assert runtime_config.env_set_field_names(unit_env_path()) == {"ops_concurrency"}
    assert unit_env_aliases()["MEIVA_OPS_CONCURRENCY"] == "4"


def test_config_write_op_absent_key_left_untouched(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Reducer semantics: a key ABSENT from the patch is left as-is. A partial
    write never unsets a field it didn't name — the full-replace footgun that
    once wiped a cluster's secrets is gone."""
    machine_identity.set(name="test-machine")
    config_write_op(
        {"ops_concurrency": 4, "machine_description": "hi"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    # A patch touching only ops_concurrency must NOT drop machine_description.
    result = config_write_op(
        {"ops_concurrency": 5},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is True
    assert runtime_config.env_set_field_names(unit_env_path()) == {
        "ops_concurrency",
        "machine_description",
    }
    assert unit_env_aliases()["MEIVA_OPS_CONCURRENCY"] == "5"


def test_config_write_op_empty_patch_is_noop(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """An empty patch touches nothing — it does not unset previously-set fields."""
    machine_identity.set(name="test-machine")
    config_write_op(
        {"ops_concurrency": 4},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    result = config_write_op(
        {}, authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    assert result.applied is True
    assert runtime_config.env_set_field_names(unit_env_path()) == {"ops_concurrency"}


def test_config_write_op_does_not_disturb_non_managed_env(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A write touches only the fields named in the patch — a connection value
    the first start wrote into .env (e.g. MEIVA_DB_URL) is left alone even when a
    managed field is explicitly unset."""
    machine_identity.set(name="test-machine")
    unit_home.joinpath(".env").write_text("MEIVA_DB_URL=postgresql://x@127.0.0.1:1/x\n")
    config_write_op(
        {"ops_concurrency": 4},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    config_write_op(
        {"ops_concurrency": None},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )  # explicit unset must not touch MEIVA_DB_URL
    assert unit_env_aliases()["MEIVA_DB_URL"] == "postgresql://x@127.0.0.1:1/x"
    assert "MEIVA_OPS_CONCURRENCY" not in unit_env_aliases()


def test_config_write_op_restart_required_union(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """restart_required is the sorted union over written fields' restart_required values."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {"ops_concurrency": 4, "machine_description": "hi"},
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )
    assert result.applied is True
    # ops_concurrency.restart_required + machine_description.restart_required
    metas = {m.name: m for m in panel_metadata(authority=config_authority)}
    expected = sorted(
        {
            v
            for f in ("ops_concurrency", "machine_description")
            for v in [metas[f].restart_required]
            if v
        }
    )
    assert result.restart_required == expected


def test_config_write_op_empty_overrides_applied_no_write(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Empty overrides dict -> applied=True, nothing written, restart_required=[]."""
    machine_identity.set(name="test-machine")
    result = config_write_op(
        {}, authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    assert result.applied is True
    assert result.restart_required == []
    assert unit_env_aliases() == {}


def test_config_write_op_rejects_an_invalid_candidate_without_writing(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """A value the settings model would refuse at startup is never persisted."""
    runtime_config.write_fields({"db_url": "postgresql://meiva@127.0.0.1:5432/meiva"}, set())
    env_path = unit_home / ".env"
    before = env_path.read_bytes()

    result = config_write_op(
        {"redis_bin_dir": "relative/bin"},
        local=True,
        authority=config_authority,
        local_machine=machine_identity.name(),
        host=fake_host(),
    )

    assert result.applied is False
    assert result.results["redis_bin_dir"].ok is False
    assert result.results["redis_bin_dir"].reason is not None
    assert "candidate rejected" in result.results["redis_bin_dir"].reason
    assert env_path.read_bytes() == before


def test_config_write_op_machine_name_in_result(
    unit_home: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Result includes the explicitly supplied local machine name."""
    machine_identity.set(name="my-host")
    result = config_write_op(
        {}, authority=config_authority, local_machine=machine_identity.name(), host=fake_host()
    )
    assert result.machine == "my-host"


# ---------------------------------------------------------------------------
# _dispatch routing for config_read and config_write
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# config_audit_read_op
# ---------------------------------------------------------------------------


def test_config_audit_read_op_returns_this_hosts_newest_records(
    unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    from ops.env_audit import record_env_write

    machine_identity.set(name="test-machine")
    env_path = unit_home / ".env"
    env_path.write_text("MEIVA_MODEL=m\n")
    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="one")
    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="two")

    result = config_audit_read_op(1, local_machine=machine_identity.name())
    assert result.machine == "test-machine"
    assert [record["site"] for record in result.records] == ["two"]
