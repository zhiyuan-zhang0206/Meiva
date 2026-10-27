# meiva:note
# The native receiver of one immutable upload batch: reserve quota, pull the bytes the
# gateway holds, verify the files on disk, then acknowledge. It never overwrites a final.
#
# `copy_batch` is the shell: it takes every environmental fact as a value in its
# `ReceiverConfig` and executes the plan `copy_plan` returns. The ops daemon builds the config once
# at boot, where it reads the home, the OS user's home, the gateway access and the quota constants.

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from psycopg import Connection
from psycopg.types.json import Jsonb

from base.agents.upload_delivery import storage
from base.agents.upload_delivery.models import (
    NAMESPACE,
    CopyProof,
    Manifest,
    ReceiveRequest,
    UploadDeliveryConflictError,
)
from base.agents.upload_delivery.paths import (
    agent_upload_dir,
)
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.db.transaction import ConnectionSource, write_transaction
from base.host.db_authority.unit import UnitIdentity
from base.host.net.http_dial import get as http_get
from ops.upload_delivery.copy_plan import (
    Admission,
    FetchObject,
    ObjectStep,
    ReuseObject,
    admission,
    fetch_timeout_s,
    plan_objects,
)

__all__ = ["ReceiverConfig", "copy_batch"]


# meiva:note
# Everything a receiver reads from its environment, read once by its caller.
#
# `unit` is this receiver's native machine and resolved `MEIVA_HOME`; `user_home` is the OS
# user's home, under which `Downloads/MeivaAgent-N` lives independently of `MEIVA_HOME`;
# `access` reaches the gateway; the quota is what one agent may hold on the native root. The
# network round of one batch is bounded by `fetch_round_s`, and a single request by
# `fetch_request_cap_s`.
@dataclass(frozen=True)
class ReceiverConfig:
    unit: UnitIdentity
    user_home: Path
    access: GatewayAccess
    quota_bytes: int
    quota_files: int
    fetch_round_s: float = 120.0
    fetch_request_cap_s: float = 30.0


# meiva:note
# Reserve native quota, pull immutable bytes, verify the actual files, then acknowledge.
#
# Ready evidence is historical, so a repeat verifies the actual files before it returns any
# proof, and fetches only the objects that are absent; a conflicting present final is never
# overwritten. The database connection is released across every network and disk phase.
def copy_batch(
    pool: ConnectionSource, request: ReceiveRequest, config: ReceiverConfig
) -> CopyProof:
    if request.target != config.unit:
        raise UploadDeliveryConflictError("copy targets a different native machine/home")
    manifest = request.manifest
    directory = agent_upload_dir(manifest.agent_id, create=False, home=config.user_home).resolve()
    _reserve(pool, request, config, directory)
    contents = _gather(manifest, directory, config)
    storage.publish(directory, manifest, contents)
    storage.verify_all(directory, manifest)
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE upload_delivery_copies SET ready_at=now() "
            "WHERE batch_id=%s AND storage_machine=%s AND unit_home=%s",
            (manifest.batch_id, config.unit.machine, config.unit.home),
        )
    return CopyProof(
        version=1,
        target=config.unit,
        manifest_hash=manifest.fingerprint(),
        directory=str(directory / NAMESPACE / manifest.batch_id),
    )


def _reserve(
    pool: ConnectionSource, request: ReceiveRequest, config: ReceiverConfig, directory: Path
) -> None:
    manifest = request.manifest
    unit = config.unit
    with write_transaction(pool) as conn:
        storage.lock_agent(conn, manifest.agent_id)
        row = conn.execute(
            "SELECT request, storage_directory FROM upload_delivery_copies "
            "WHERE batch_id=%s AND storage_machine=%s AND unit_home=%s",
            (manifest.batch_id, unit.machine, unit.home),
        ).fetchone()
        verdict = admission(
            row_exists=row is not None,
            same_request=row is not None and row[0] == request.model_dump(),
            same_directory=row is not None and row[1] == str(directory),
        )
        match verdict:
            case Admission.CONFLICT:
                raise UploadDeliveryConflictError(
                    "copy identity already binds another manifest or native root"
                )
            case Admission.NEW:
                _check_quota(conn, manifest, config, directory)
                conn.execute(
                    "INSERT INTO upload_delivery_copies "
                    "(batch_id,unit_home,agent_id,request,manifest,storage_machine,"
                    "storage_directory) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        manifest.batch_id,
                        unit.home,
                        manifest.agent_id,
                        Jsonb(request.model_dump()),
                        Jsonb(manifest.model_dump()),
                        unit.machine,
                        str(directory),
                    ),
                )
            case Admission.RESUME:
                _check_quota(conn, manifest, config, directory)
                conn.execute(
                    "UPDATE upload_delivery_copies SET ready_at=NULL "
                    "WHERE batch_id=%s AND storage_machine=%s AND unit_home=%s",
                    (manifest.batch_id, unit.machine, unit.home),
                )
            case _:
                assert_never(verdict)


def _check_quota(
    conn: Connection, manifest: Manifest, config: ReceiverConfig, directory: Path
) -> None:
    storage.check_quota(
        conn,
        manifest.agent_id,
        directory,
        config.unit.machine,
        sum(item.size for item in manifest.objects),
        len(manifest.objects),
        config.quota_bytes,
        config.quota_files,
        incoming_manifest=manifest,
    )


def _gather(manifest: Manifest, directory: Path, config: ReceiverConfig) -> list[bytes]:
    paths = [storage.object_path(directory, manifest, item) for item in manifest.objects]
    steps = plan_objects(
        tuple(path.exists() or path.is_symlink() for path in paths),
        agent_id=manifest.agent_id,
        batch_id=manifest.batch_id,
    )
    deadline = time.monotonic() + config.fetch_round_s
    return [_gather_object(step, manifest, paths, deadline, config) for step in steps]


def _gather_object(
    step: ObjectStep, manifest: Manifest, paths: list[Path], deadline: float, config: ReceiverConfig
) -> bytes:
    match step:
        case ReuseObject(ordinal=ordinal):
            storage.verify(paths[ordinal], manifest.objects[ordinal])
            return paths[ordinal].read_bytes()
        case FetchObject(path=path):
            timeout = fetch_timeout_s(
                remaining_s=deadline - time.monotonic(), request_cap_s=config.fetch_request_cap_s
            )
            if timeout is None:
                raise TimeoutError("immutable upload copy exhausted its bounded network round")
            response = http_get(
                gateway_api_base(config.access.gateway_url) + path,
                headers=gateway_auth_headers(config.access),
                timeout=timeout,
            )
            response.raise_for_status()
            return response.content
        case _:
            assert_never(step)
