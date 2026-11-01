# meiva:note
# Config and inventory read-modify-write operations share a thread lock so simultaneous workers cannot overwrite one another's fields or secrets. An event-loop lock cannot serialize these worker threads. This lock is process-local; the runtime config owner retains cross-process file locking. Blocking operations use the daemon-owned executor.
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from base.agents.to_user import ToUserReadRequest
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.daemon.endpoints import ServiceEndpoint
from base.db import Database
from base.host.system.hostport import Host
from base.native_process.loaded_commit import LoadedCommit
from ops import host_config, inventory
from ops.cluster import operations as cluster
from ops.rpc_schemas import (
    AgentSkillViewPayload,
    ConfigAuditReadPayload,
    ConfigWritePayload,
    InventoryWritePayload,
    OpStatus,
    ShellCapturePayload,
    ShellKillPayload,
    ShellProbePayload,
)
from ops.to_user import read_failure_payload, read_to_user
from ops.upload_delivery import ReceiverConfig, copy_batch
from services.agent_runner.agent_ops.ops_plan import unknown_kind

_state_write_lock = threading.Lock()


def dispatch_sync(
    kind: str,
    payload: dict[str, Any],
    *,
    pool: ConnectionPool | None,
    db: Database,
    authority: ConfigAuthority,
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    home: Path,
    receiver: ReceiverConfig,
    agent_host: ServiceEndpoint,
    now: datetime,
) -> tuple[OpStatus, dict[str, object]]:

    match kind:
        case "to_user_read":
            request = ToUserReadRequest.model_validate(payload)
            if pool is None:
                raise RuntimeError("To user requires the native DB pool")
            try:
                document = read_to_user(
                    pool,
                    request.agent_id,
                    local_machine.name(),
                    references=request.references,
                    home=home,
                )
            except (OSError, ValueError) as exc:
                # meiva:note
                # A refused read is the answer, not a crash: the class it carries is
                # what the gateway turns into the panel's words.
                # meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
                return OpStatus.FAILED, read_failure_payload(exc)
            return OpStatus.COMPLETED, document.model_dump(mode="json")
        case "upload-receive-v2":
            return _upload_arm(payload, pool, receiver=receiver)
        case "status_probe":
            return OpStatus.COMPLETED, cluster.cluster_status_op(
                db,
                pool,
                image=image,
                local_machine=local_machine,
                home=home,
                agent_host_pidfile=agent_host.pidfile,
            ).model_dump(mode="json")
        case "config_read":
            return OpStatus.COMPLETED, host_config.config_read_op(
                authority=authority, local_machine=local_machine.name(), host=host, home=home
            ).model_dump(mode="json")
        case "config_audit_read":
            ca = ConfigAuditReadPayload.model_validate(payload)
            return OpStatus.COMPLETED, host_config.config_audit_read_op(
                ca.last, local_machine=local_machine.name()
            ).model_dump(mode="json")
        case "config_write":
            cw = ConfigWritePayload.model_validate(payload)
            with _state_write_lock:
                return OpStatus.COMPLETED, host_config.config_write_op(
                    cw.overrides,
                    authority=authority,
                    local=cw.local,
                    actor=cw.actor,
                    trace_id=cw.trace_id,
                    local_machine=local_machine.name(),
                    host=host,
                    home=home,
                ).model_dump(mode="json")
        case "inventory_read":
            return OpStatus.COMPLETED, inventory.inventory_read_op(
                local_machine=local_machine, host=host, home=home
            ).model_dump(mode="json")
        case "inventory_write":
            iw = InventoryWritePayload.model_validate(payload)
            with _state_write_lock:
                return OpStatus.COMPLETED, inventory.inventory_write_op(
                    iw.plugins, iw.mcp_servers, local_machine=local_machine, host=host, home=home
                ).model_dump(mode="json")
        case "harness_probe":
            return OpStatus.COMPLETED, cluster.harness_probe_op(local_machine.name()).model_dump(
                mode="json"
            )
        case (
            "shell_probe" | "shell_kill" | "shell_capture" | "schedule_capture" | "agent_skill_view"
        ):
            return OpStatus.COMPLETED, _agent_arm(kind, payload, pool, home=home, now=now)
        case (
            "workspace_transfer_prepare"
            | "workspace_transfer_import"
            | "workspace_transfer_finalize"
            | "workspace_transfer_abort"
        ):
            return _transfer_arm(
                kind,
                payload,
                local_machine=local_machine.name(),
                home=home,
                receiver=receiver,
            )
        case _:
            return unknown_kind(kind)


def _agent_arm(
    kind: str,
    payload: dict[str, Any],
    pool: ConnectionPool | None,
    *,
    home: Path,
    now: datetime,
) -> dict[str, object]:

    match kind:
        case "shell_probe":
            sp = ShellProbePayload.model_validate(payload)
            return cluster.shell_probe_op(sp.agent_id, home=home, now=now).model_dump(mode="json")
        case "shell_kill":
            sk = ShellKillPayload.model_validate(payload)
            return cluster.shell_kill_op(sk.agent_id, sk.session_id, home=home).model_dump(
                mode="json"
            )
        case "agent_skill_view":
            asv = AgentSkillViewPayload.model_validate(payload)
            if pool is None:
                raise RuntimeError("agent skill view requires the native DB pool")
            return cluster.agent_skill_view_op(asv.agent_id, pool, home=home).model_dump(
                mode="json"
            )
        case "shell_capture":
            sc = ShellCapturePayload.model_validate(payload)
            return cluster.shell_capture_op(
                sc.agent_id, sc.session_id, sc.lines, home=home, now=now
            ).model_dump(mode="json")
        case "schedule_capture":
            from ops.rpc_schemas import ScheduleCapturePayload

            scp = ScheduleCapturePayload.model_validate(payload)
            return cluster.schedule_capture_op(scp.schedule_id, scp.lines, home=home).model_dump(
                mode="json"
            )
        case _:
            raise ValueError(f"not an agent arm: {kind!r}")


def _upload_arm(
    payload: dict[str, Any], pool: ConnectionPool | None, *, receiver: ReceiverConfig
) -> tuple[OpStatus, dict[str, object]]:

    from httpx import HTTPStatusError

    from base.agents.upload_delivery.models import (
        ReceiveRequest,
        UploadDeliveryConflictError,
        UploadQuotaExceededError,
    )

    if pool is None:
        raise RuntimeError("immutable upload receiver requires the native DB pool")
    request = ReceiveRequest.model_validate(payload)
    try:
        return OpStatus.COMPLETED, copy_batch(pool, request, receiver).model_dump(mode="json")
    except HTTPStatusError as exc:
        if exc.response.status_code in {401, 403, 404, 409}:
            return OpStatus.FAILED, {
                "error": "source object unavailable",
                "reason": "upload-source-unavailable-v1",
            }
        raise
    except UploadDeliveryConflictError:
        return OpStatus.FAILED, {
            "error": "immutable copy conflict",
            "reason": "upload-copy-conflict-v1",
        }
    except UploadQuotaExceededError:
        return OpStatus.FAILED, {
            "error": "native quota exceeded",
            "reason": "upload-copy-quota-v1",
        }


def _transfer_arm(
    kind: str,
    payload: dict[str, Any],
    *,
    local_machine: str,
    home: Path,
    receiver: ReceiverConfig,
) -> tuple[OpStatus, dict[str, object]]:
    from httpx import HTTPStatusError

    from ops.agent_transfer import WorkspaceTransferError
    from ops.agent_transfer.operations import (
        abort_transfer_op,
        finalize_transfer_op,
        import_workspace_op,
        prepare_target_op,
    )
    from ops.rpc_schemas import (
        WorkspaceTransferAbortPayload,
        WorkspaceTransferFinalizePayload,
        WorkspaceTransferImportPayload,
        WorkspaceTransferPreparePayload,
    )

    try:
        match kind:
            case "workspace_transfer_prepare":
                prep = WorkspaceTransferPreparePayload.model_validate(payload)
                return OpStatus.COMPLETED, prepare_target_op(
                    prep, local_machine=local_machine, home=home
                ).model_dump(mode="json")
            case "workspace_transfer_import":
                imp = WorkspaceTransferImportPayload.model_validate(payload)
                return OpStatus.COMPLETED, import_workspace_op(
                    imp,
                    local_machine=local_machine,
                    home=home,
                    access=receiver.access,
                ).model_dump(mode="json")
            case "workspace_transfer_finalize":
                fin = WorkspaceTransferFinalizePayload.model_validate(payload)
                return OpStatus.COMPLETED, finalize_transfer_op(
                    fin, local_machine=local_machine, home=home
                ).model_dump(mode="json")
            case "workspace_transfer_abort":
                abt = WorkspaceTransferAbortPayload.model_validate(payload)
                return OpStatus.COMPLETED, abort_transfer_op(
                    abt, local_machine=local_machine, home=home
                ).model_dump(mode="json")
            case _:
                raise ValueError(f"not a transfer arm: {kind!r}")
    except HTTPStatusError as exc:
        if exc.response.status_code in {401, 403, 404, 409, 413}:
            return OpStatus.FAILED, {
                "error": f"gateway bundle fetch returned HTTP {exc.response.status_code}",
                "reason": "transfer_bundle_unavailable",
            }
        raise
    except WorkspaceTransferError as exc:
        return OpStatus.FAILED, {
            "error": str(exc),
            "reason": exc.reason,
        }
