# meiva:note
# Runner-side operations for whole-agent cross-machine transfer:
# - `export_workspace_bundle`: serves `POST /workspace/export` on the source runner after verifying
#   Postgres placement and non-running fence state, killing local persistent shells below the
#   snapshot `session_index`, and packing `<home>/workspaces/<agent_id>`.
# - `prepare_target_op`, `import_workspace_op`, `finalize_transfer_op`, `abort_transfer_op`: serve
#   the four `POST /ops` arms on target and source runners.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from base import paths
from base.agents.lifecycle import release_browser_pages
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net.http_dial import get as http_get
from ops.agent_transfer.archive_io import (
    abort_target_workspace,
    finalize_workspace,
    pack_workspace,
    prepare_target_workspace,
    stage_and_activate_workspace,
)
from ops.agent_transfer.bundle_plan import (
    MAX_BUNDLE_STREAM_BYTES,
    WorkspaceTransferConflictError,
    WorkspaceTransferCorruptError,
    WorkspaceTransferTooLargeError,
)
from ops.cluster_status import agent_shell_sessions, kill_agent_shells, kill_shell
from ops.rpc_schemas.transfer import (
    WorkspaceExportRequest,
    WorkspaceTransferAbortPayload,
    WorkspaceTransferAbortResult,
    WorkspaceTransferFinalizePayload,
    WorkspaceTransferFinalizeResult,
    WorkspaceTransferImportPayload,
    WorkspaceTransferImportResult,
    WorkspaceTransferPreparePayload,
    WorkspaceTransferPrepareResult,
)

_log = logging.getLogger(__name__)
_FETCH_TIMEOUT_S = 90.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _verify_export_fence(
    pool: ConnectionPool,
    request: WorkspaceExportRequest,
    *,
    local_machine: str,
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, status, runtime_owner, runtime_generation, "
            "last_admission_at, session_index "
            "FROM agents_meta WHERE id = %s",
            (request.agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceTransferConflictError(f"agent {request.agent_id} does not exist")
        machine, status, runtime_owner, runtime_generation, last_admission_at, session_index = row
        if machine != local_machine:
            raise WorkspaceTransferConflictError(
                f"agent {request.agent_id} is hosted on {machine!r}, not {local_machine!r}"
            )
        if status == "running":
            raise WorkspaceTransferConflictError(
                f"agent {request.agent_id} is running on {local_machine!r}; transfer refuses running agents"
            )
        if (
            status != request.expected_status
            or runtime_owner != request.expected_runtime_owner
            or runtime_generation != request.expected_runtime_generation
            or last_admission_at != request.expected_last_admission_at
            or int(session_index) != request.expected_session_index
        ):
            raise WorkspaceTransferConflictError(
                f"agent {request.agent_id} state changed on {local_machine!r} before workspace export"
            )
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s "
            "AND kind IN ('restart', 'terminate') AND status IN ('pending', 'claimed') LIMIT 1",
            (request.agent_id,),
        )
        if cur.fetchone() is not None:
            raise WorkspaceTransferConflictError(
                f"agent {request.agent_id} has an in-flight lifecycle command"
            )


# meiva:note
# Verify the source fence, kill local persistent shells below `expected_session_index`, pack the
# workspace into a `MEIVA_WS_V1` bundle, and re-verify the fence so a turn admitted mid-pack aborts
# before returning bytes.
def export_workspace_bundle(
    pool: ConnectionPool,
    request: WorkspaceExportRequest,
    *,
    local_machine: str,
    home: Path,
) -> bytes:
    _verify_export_fence(pool, request, local_machine=local_machine)
    killed = kill_agent_shells(
        request.agent_id,
        before_session_index=request.expected_session_index,
        home=home,
    )
    _manifest, stream = pack_workspace(
        home=home,
        agent_id=request.agent_id,
        transfer_id=request.transfer_id,
        source_machine=local_machine,
        killed_shell_sessions=killed,
    )
    _verify_export_fence(pool, request, local_machine=local_machine)
    return stream


def prepare_target_op(
    payload: WorkspaceTransferPreparePayload,
    *,
    local_machine: str,
    home: Path,
) -> WorkspaceTransferPrepareResult:
    ws_dir, had_existing = prepare_target_workspace(
        home=home,
        agent_id=payload.agent_id,
        transfer_id=payload.transfer_id,
    )
    return WorkspaceTransferPrepareResult(
        machine=local_machine,
        workspace_dir=str(ws_dir),
        had_existing_workspace=had_existing,
    )


# meiva:note
# Pull the staged `MEIVA_WS_V1` bundle from the gateway over authenticated HTTP, verify outer stream
# length and sha256, then extract, rescan on disk, and activate `<home>/workspaces/<agent_id>`.
def import_workspace_op(
    payload: WorkspaceTransferImportPayload,
    *,
    local_machine: str,
    home: Path,
    access: GatewayAccess,
) -> WorkspaceTransferImportResult:
    if payload.bundle_bytes > MAX_BUNDLE_STREAM_BYTES:
        raise WorkspaceTransferTooLargeError(
            f"bundle size {payload.bundle_bytes} exceeds stream limit"
        )
    url = gateway_api_base(access.gateway_url) + payload.bundle_path
    response = http_get(
        url,
        headers=gateway_auth_headers(access),
        timeout=_FETCH_TIMEOUT_S,
    )
    response.raise_for_status()
    bundle_bytes = response.content
    if len(bundle_bytes) != payload.bundle_bytes:
        raise WorkspaceTransferCorruptError(
            f"fetched bundle length {len(bundle_bytes)} does not match expected {payload.bundle_bytes}"
        )
    actual_sha = hashlib.sha256(bundle_bytes).hexdigest()
    if actual_sha != payload.bundle_sha256:
        raise WorkspaceTransferCorruptError("fetched bundle sha256 mismatch")

    manifest = stage_and_activate_workspace(
        home=home,
        agent_id=payload.agent_id,
        transfer_id=payload.transfer_id,
        bundle_bytes=bundle_bytes,
    )
    return WorkspaceTransferImportResult(
        machine=local_machine,
        workspace_dir=str(paths.workspace_path(home, payload.agent_id)),
        files_transferred=manifest.files_count,
        bytes_transferred=manifest.total_file_bytes,
    )


def _cleanup_source_sessions_best_effort(agent_id: int, *, home: Path) -> None:
    try:
        for shell in agent_shell_sessions(agent_id, now=_EPOCH, home=home):
            kill_shell(agent_id, shell.id, home=home)
    except Exception:
        _log.warning(
            "post-transfer residual shell cleanup failed for agent %d", agent_id, exc_info=True
        )
    try:
        asyncio.run(release_browser_pages(agent_id, home=home))
    except Exception:
        _log.warning(
            "post-transfer browser page release failed for agent %d", agent_id, exc_info=True
        )


def finalize_transfer_op(
    payload: WorkspaceTransferFinalizePayload,
    *,
    local_machine: str,
    home: Path,
) -> WorkspaceTransferFinalizeResult:
    cleaned = finalize_workspace(
        home=home,
        agent_id=payload.agent_id,
        transfer_id=payload.transfer_id,
        role=payload.role,
    )
    if payload.role == "source":
        _cleanup_source_sessions_best_effort(payload.agent_id, home=home)
    return WorkspaceTransferFinalizeResult(machine=local_machine, cleaned=cleaned)


def abort_transfer_op(
    payload: WorkspaceTransferAbortPayload,
    *,
    local_machine: str,
    home: Path,
) -> WorkspaceTransferAbortResult:
    restored = abort_target_workspace(
        home=home,
        agent_id=payload.agent_id,
        transfer_id=payload.transfer_id,
    )
    return WorkspaceTransferAbortResult(machine=local_machine, restored_previous=restored)
