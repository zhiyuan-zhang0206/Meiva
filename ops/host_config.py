# meiva:note
# Host-scope config read / write RPC ops.
#
# Read this machine's host-scope config fields (sensitive values masked) and apply a validated
# all-or-nothing override write. One of the op clusters beside `ops.lifecycle`,
# `ops.cluster.operations` and `ops.inventory`; each cluster is self-contained.
#
# Dispatched by the agent-runner ops server (`services/agent_runner/agent_ops/daemon.py`) and
# called by the gateway config router. `SENSITIVE_MASK` is re-imported by the router to render
# masked fields consistently. The unit's `.env` path and the configured Chrome binary come from
# the `ConfigAuthority` every op takes, never from process-wide settings.

from __future__ import annotations

from pathlib import Path

from base.config import field_domain, runtime_config
from base.config.admin.candidate import validate_env_patch_for_write
from base.config.admin.editing import coerce_config_scalar, field_editable, split_reducer_patch
from base.config.admin.metadata import ConfigFieldMeta
from base.config.env_changed import EnvChangedError
from base.config.service_read import ConfigAuthority
from base.host import config_validators
from base.host.env.dotenv_file import process_env_file
from base.host.system.hostport import Host
from base.packages.plugin_config_admin import (
    panel_metadata,
    panel_overrides,
    patch_owner,
    write_plugin_patch,
)
from base.packages.plugin_config_images import PluginConfigChangedError, PluginConfigOwner
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from ops.env_audit import EnvWriteAudit, read_env_write_records
from ops.rpc_schemas import (
    ConfigAuditReadResult,
    ConfigReadResult,
    ConfigWriteOpResult,
    FieldWriteResult,
    HostConfigField,
)

SENSITIVE_MASK = "••••••••"


# meiva:note
# The configured Chrome binary override (`MEIVA_CHROME_BINARY`) as this unit's authority holds it.
def _chrome_binary(authority: ConfigAuthority) -> str | None:
    return authority.service_field_value("chrome_binary")


# meiva:note
# Read this machine's host-scope config fields and return them with metadata.
#
# Returns a ConfigReadResult with:
#   - machine: this machine's name
#   - host_fields: one entry per host-scope field with effective value (sensitive fields
#     masked), set-in-.env flag, remote_writable flag, and optional read-time capability hint
#   - raw_overrides: the host's editable override set ({field: value} for the remotely-writable
#     host fields set in this machine's .env), sensitive keys omitted (the frontend deltas
#     against this to build a write payload)
def config_read_op(
    *, authority: ConfigAuthority, local_machine: str, host: Host
) -> ConfigReadResult:
    metas = panel_metadata(authority=authority)
    metas_by_name = {m.name: m for m in metas}
    chrome_binary = _chrome_binary(authority)
    set_fields = set(runtime_config.env_set_field_names(authority.env_path))
    set_fields.update(panel_overrides(authority=authority))
    overrides = panel_overrides(authority=authority)
    host_fields: dict[str, HostConfigField] = {}
    for meta in metas:
        if meta.scope != "host":
            continue
        value = meta.current_value
        if meta.sensitive and value:
            value = SENSITIVE_MASK
        cap = config_validators.read_time_capability(
            meta.name, chrome_binary=chrome_binary, host=host
        )
        host_fields[meta.name] = HostConfigField(
            value=value,
            overridden=meta.name in set_fields,
            remote_writable=meta.remote_writable,
            can_enable=cap.ok if cap is not None else None,
            reason=cap.reason if cap is not None else None,
        )

    raw_overrides = {
        name: value
        for name, value in overrides.items()
        if metas_by_name[name].scope == "host" and not metas_by_name[name].sensitive
    }

    return ConfigReadResult(
        machine=local_machine,
        host_fields=host_fields,
        raw_overrides=raw_overrides,
    )


# meiva:note
# Read this machine's most recent `.env`-write audit records (newest first).
#
# Still resolves the process's own `.env`: its callers (the ops daemon's dispatch and the gateway's
# in-process read) hold no `ConfigAuthority` at this call yet, and each would hand
# `authority.env_path` here once their roots thread it.
def config_audit_read_op(last: int, *, local_machine: str) -> ConfigAuditReadResult:
    return ConfigAuditReadResult(
        machine=local_machine, records=read_env_write_records(last, process_env_file())
    )


# meiva:note
# Per-field gate: known, host-scope, editable here, and (unless an unset) valid.
def _field_verdicts(
    overrides: dict[str, object],
    metas: dict[str, ConfigFieldMeta],
    *,
    local: bool,
    chrome_binary: str | None,
    host: Host,
) -> dict[str, FieldWriteResult]:
    results: dict[str, FieldWriteResult] = {}
    for field, value in overrides.items():
        if field not in metas:
            results[field] = FieldWriteResult(ok=False, reason="unknown field")
            continue
        meta = metas[field]
        if meta.scope != "host":
            results[field] = FieldWriteResult(ok=False, reason="not a host-scope field")
            continue
        if not field_editable(meta, local=local):
            results[field] = FieldWriteResult(
                ok=False, reason="not editable" if local else "not remotely editable"
            )
            continue
        if value is None:
            # meiva:note
            # An explicit unset has no value to validate.
            results[field] = FieldWriteResult(ok=True, reason=None)
            continue
        if meta.owner is not None:
            try:
                coerce_config_scalar(meta.field_type, value, meta.choices)
                results[field] = FieldWriteResult(ok=True, reason=None)
            except (ValueError, TypeError):
                results[field] = FieldWriteResult(ok=False, reason="invalid declared scalar")
            continue
        vr = config_validators.validate(field, value, chrome_binary=chrome_binary, host=host)
        results[field] = FieldWriteResult(ok=vr.ok, reason=vr.reason)
    return results


# meiva:note
# Fail the fields whose domain the candidate rejected (the first patched field if none).
def _reject_candidate(
    errors_by_domain: dict[str, list[str]],
    patched_fields: set[str],
    results: dict[str, FieldWriteResult],
) -> None:
    rejected_fields = {field for field in patched_fields if field_domain(field) in errors_by_domain}
    if not rejected_fields:
        rejected_fields = {next(iter(sorted(patched_fields)))}
    for field in rejected_fields:
        errors = errors_by_domain.get(field_domain(field))
        reason = "candidate rejected: " + "; ".join(errors or [])
        results[field] = FieldWriteResult(ok=False, reason=reason)


# meiva:note
# Candidate-validate then write the patch to `env_path`; `(applied, restart_required)`.
#
# A rejected candidate or a lost concurrent-write race flips the affected fields in `results`.
def _apply_patch(
    overrides: dict[str, object],
    metas: dict[str, ConfigFieldMeta],
    results: dict[str, FieldWriteResult],
    *,
    env_path: Path,
    actor: str | None,
    trace_id: str | None,
) -> tuple[bool, list[str]]:
    writes, removals = split_reducer_patch(overrides, metas)
    candidate = validate_env_patch_for_write(writes, removals, path=env_path)
    errors_by_domain = candidate.errors_by_domain
    if errors_by_domain:
        _reject_candidate(errors_by_domain, set(writes) | removals, results)
        return False, []
    applied = True
    if writes or removals:
        try:
            runtime_config.write_fields(
                writes,
                removals,
                expected_digest=candidate.expected_digest,
                audit=EnvWriteAudit(site="ops_config_write", actor=actor, trace_id=trace_id),
                path=env_path,
            )
        except EnvChangedError:
            applied = False
            for field in set(writes) | removals:
                results[field] = FieldWriteResult(
                    ok=False,
                    reason="config changed concurrently; retry the request",
                )
    restart_required = sorted(
        {
            metas[f].restart_required
            for f in set(writes) | removals
            if applied and metas[f].restart_required
        }
    )
    return applied, restart_required


# meiva:note
# Commit one schema owner; report candidate or concurrent-write rejection.
def _apply_plugin_patch(
    owner: PluginConfigOwner,
    overrides: dict[str, object],
    metas: dict[str, ConfigFieldMeta],
    results: dict[str, FieldWriteResult],
) -> tuple[bool, list[str]]:
    writes, removals = split_reducer_patch(overrides, metas)
    typed = {
        k: coerce_config_scalar(metas[k].field_type, v, metas[k].choices) for k, v in writes.items()
    }
    try:
        write_plugin_patch(owner, typed, removals, expected_digest=None)
    except (InvalidConfigOverlay, PluginConfigChangedError) as exc:
        for field in overrides:
            results[field] = FieldWriteResult(ok=False, reason=str(exc))
        return False, []
    return True, sorted({metas[f].restart_required for f in overrides if metas[f].restart_required})


# meiva:note
# Validate and merge this machine's host overrides into its .env (reducer semantics).
#
# `overrides` is a JSON-merge-patch over the host `.env`: a key with a value is
# set/replaced, a key mapped to `None` is unset (reverted to its default), and a
# key that is ABSENT is left untouched. Deletion is always explicit (a null
# value) — never inferred from absence — so a partial payload can never silently
# drop a field it did not mention (a full-replace once wiped a cluster's secrets
# that way). Validates every field against host-scope + editability checks, then
# the host-side capability validator (skipped for an unset — there is no value to
# validate). Only writes if every field passes (atomic). Returns per-field
# results, applied flag, and the union of restart_required values from the
# changed fields.
#
# `local` distinguishes a self-target edit (the gateway editing its OWN host
# fields) from a remote one. A host field is editable iff `writable` when local,
# else `remote_writable` — `writable` means "a human may edit it on its own host",
# `remote_writable` is the narrower allowlist for editing a *remote* host's field.
# The gate is `base.config.admin.editing.field_editable` — the same definition the
# gateway's PUT /api/config gate uses, so the two write paths cannot drift.
def config_write_op(
    overrides: dict[str, object],
    *,
    authority: ConfigAuthority,
    local: bool = False,
    actor: str | None = None,
    trace_id: str | None = None,
    local_machine: str,
    host: Host,
) -> ConfigWriteOpResult:
    metas = {m.name: m for m in panel_metadata(authority=authority)}
    try:
        owner = patch_owner(set(overrides))
    except InvalidConfigOverlay as exc:
        return ConfigWriteOpResult(
            machine=local_machine,
            results={f: FieldWriteResult(ok=False, reason=str(exc)) for f in overrides},
            applied=False,
            restart_required=[],
        )

    results = _field_verdicts(
        overrides, metas, local=local, chrome_binary=_chrome_binary(authority), host=host
    )

    # meiva:note
    # `all()` over an empty dict is True, so an empty payload is trivially applied.
    applied = all(r.ok for r in results.values())

    restart_required: list[str] = []
    if applied and owner is not None:
        applied, restart_required = _apply_plugin_patch(owner, overrides, metas, results)
    elif applied:
        applied, restart_required = _apply_patch(
            overrides, metas, results, env_path=authority.env_path, actor=actor, trace_id=trace_id
        )

    return ConfigWriteOpResult(
        machine=local_machine,
        results=results,
        applied=applied,
        restart_required=restart_required,
    )
