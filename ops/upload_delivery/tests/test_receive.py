# meiva:note
# The receiver's shell against real resources: the throwaway Postgres, a loopback ASGI
# application that plays the gateway's object endpoint, and files under `tmp_path`. Nothing
# is patched; faults come from a trigger on the real table, a removed object and a spent round.

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.upload_delivery.models import (
    NAMESPACE,
    CopyProof,
    ReceiveRequest,
    UploadDeliveryConflictError,
    UploadQuotaExceededError,
    make_manifest,
)
from base.agents.upload_delivery.paths import delivered_upload_dir
from base.cluster.machine import GatewayAccess
from base.db import Database
from base.host.db_authority.unit import UnitIdentity
from ops.tests.db_faults import failing_write
from ops.upload_delivery import ReceiverConfig, copy_batch
from ops.upload_delivery.tests.object_server import serve_objects

_AGENT = 7
_SOURCE = UnitIdentity(machine="gateway-box", home="/gateway/home")


# meiva:note
# The receiving machine for one test: its own home under `tmp_path`, because the delivered copy
# lands inside that home's agent workspace.
def _target(home: Path) -> UnitIdentity:
    return UnitIdentity(machine="runner-a", home=str(home / "meiva-home"))


# meiva:note
# An isolated test credential: the loopback gateway stand-in accepts any bearer and records it.
_TOKEN = "runner-api-token"  # noqa: S105


@dataclass(frozen=True)
class Batch:
    request: ReceiveRequest
    contents: dict[int, bytes]

    def stored(self, config: ReceiverConfig, ordinal: int) -> Path:
        directory = delivered_upload_dir(
            _AGENT, home=Path(config.unit.home), name=config.upload_dir_name, create=False
        ).resolve()
        item = self.request.manifest.objects[ordinal]
        return directory / NAMESPACE / self.request.manifest.batch_id / item.name


def _batch(home: Path, *contents: bytes) -> Batch:
    manifest = make_manifest(
        _AGENT,
        [f"file-{n}.png" for n in range(len(contents))],
        [("", data, "image/png") for data in contents],
    )
    request = ReceiveRequest(version=1, target=_target(home), source=_SOURCE, manifest=manifest)
    return Batch(request, dict(enumerate(contents)))


# meiva:note
# The receiver's config for the loopback gateway at `url`; its cluster secret is an isolated test
# credential that the machine token shadows.
def _config(
    tmp_path: Path, url: str, *, quota_bytes: int = 10_000, quota_files: int = 100
) -> ReceiverConfig:
    return ReceiverConfig(
        unit=_target(tmp_path),
        upload_dir_name="uploads",
        access=GatewayAccess(
            gateway_url=url,
            cluster_secret="human-secret",  # noqa: S106
            remote_plane=False,
            machine_token=_TOKEN,
            launcher_profile=None,
            gateway_home=False,
        ),
        quota_bytes=quota_bytes,
        quota_files=quota_files,
    )


@pytest.fixture
def pool(database: Database) -> Iterator[ConnectionPool]:
    with database.pool(min_size=1, max_size=3) as pool:
        yield pool


def _copies(db_conn: psycopg.Connection) -> list[tuple[str, bool]]:
    rows = db_conn.execute(
        "SELECT batch_id, ready_at IS NOT NULL FROM upload_delivery_copies ORDER BY batch_id"
    ).fetchall()
    return [(str(batch), bool(ready)) for batch, ready in rows]


# meiva:note
# The whole path: reserve, pull each object with the machine token, publish, verify on disk,
# mark ready, and prove the native directory.
def test_a_batch_is_pulled_published_and_proven(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first", b"second")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        proof = copy_batch(pool, batch.request, config)

    assert [(pull.ordinal, pull.authorization) for pull in served.pulls] == [
        (0, f"Bearer {_TOKEN}"),
        (1, f"Bearer {_TOKEN}"),
    ]
    assert batch.stored(config, 0).read_bytes() == b"first"
    assert batch.stored(config, 1).read_bytes() == b"second"
    assert proof.target == config.unit and proof.manifest_hash == manifest.fingerprint()
    assert proof.directory == str(batch.stored(config, 0).parent)
    assert _copies(db_conn) == [(manifest.batch_id, True)]


# meiva:note
# A repeat verifies the files on disk and fetches nothing: ready evidence is historical but the
# bytes are already there.
def test_a_repeat_reuses_the_files_and_fetches_nothing(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first", b"second")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        first = copy_batch(pool, batch.request, config)
        pulled = len(served.pulls)
        again = copy_batch(pool, batch.request, config)

    assert again == first
    assert len(served.pulls) == pulled
    assert _copies(db_conn) == [(manifest.batch_id, True)]


# meiva:note
# Only the absent objects are fetched: a file that survived a partial earlier run stays, and its
# neighbour is pulled.
def test_only_the_absent_objects_are_fetched(pool: ConnectionPool, tmp_path: Path) -> None:
    batch = _batch(tmp_path, b"first", b"second")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        copy_batch(pool, batch.request, config)
        batch.stored(config, 1).unlink()
        served.pulls.clear()

        copy_batch(pool, batch.request, config)

    assert [pull.ordinal for pull in served.pulls] == [1]
    assert batch.stored(config, 1).read_bytes() == b"second"


# meiva:note
# A request for another native machine or home is refused before any reservation or pull.
def test_a_request_for_another_unit_is_refused(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    other = batch.request.model_copy(
        update={"target": _target(tmp_path).model_copy(update={"home": "/runner/a/other-home"})}
    )
    with (
        serve_objects(
            agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
        ) as served,
        pytest.raises(UploadDeliveryConflictError, match="different native machine/home"),
    ):
        copy_batch(pool, other, _config(tmp_path, served.url))

    assert served.pulls == []
    assert _copies(db_conn) == []


# meiva:note
# A batch id keeps the manifest it first reserved: the same id with a changed object hash is a
# conflict, and the first copy stays ready.
def test_a_changed_manifest_for_a_reserved_batch_conflicts(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    item = manifest.objects[0]
    changed = batch.request.model_copy(
        update={
            "manifest": manifest.model_copy(
                update={"objects": [item.model_copy(update={"sha256": "0" * 64})]}
            )
        }
    )
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        copy_batch(pool, batch.request, config)

        with pytest.raises(UploadDeliveryConflictError, match="another manifest or native root"):
            copy_batch(pool, changed, config)

    assert _copies(db_conn) == [(manifest.batch_id, True)]


# meiva:note
# The same batch against another native root is a conflict too: the reservation freezes the
# physical directory it was made for. Only the directory name can move it now — the rest of the
# path follows the unit's own home.
def test_a_reserved_batch_cannot_move_to_another_native_root(
    pool: ConnectionPool, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        copy_batch(pool, batch.request, config)

        with pytest.raises(UploadDeliveryConflictError, match="another manifest or native root"):
            copy_batch(pool, batch.request, replace(config, upload_dir_name="inbox"))


# meiva:note
# A batch beyond the native quota is refused before it reserves anything or touches the network.
def test_a_batch_beyond_the_quota_reserves_nothing(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first", b"second")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url, quota_bytes=5)

        with pytest.raises(UploadQuotaExceededError):
            copy_batch(pool, batch.request, config)

    assert served.pulls == []
    assert _copies(db_conn) == []
    assert not batch.stored(config, 0).exists()


# meiva:note
# The gateway no longer has an object: the pull fails with its HTTP status, nothing is
# published, and no ready marker is written.
def test_a_removed_source_object_fails_without_a_false_ready(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        served.gone.add(0)
        config = _config(tmp_path, served.url)

        with pytest.raises(httpx.HTTPStatusError) as raised:
            copy_batch(pool, batch.request, config)

    assert raised.value.response.status_code == 404
    assert not batch.stored(config, 0).exists()
    assert _copies(db_conn) == [(manifest.batch_id, False)]


# meiva:note
# Bytes that do not match the manifest's hash are never published: the batch is refused.
def test_bytes_that_differ_from_the_manifest_are_never_published(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects={0: b"tampered"}
    ) as served:
        config = _config(tmp_path, served.url)

        with pytest.raises(UploadDeliveryConflictError, match="differ from the fixed manifest"):
            copy_batch(pool, batch.request, config)

    assert not batch.stored(config, 0).exists()
    assert _copies(db_conn) == [(manifest.batch_id, False)]


# meiva:note
# The network round of a batch is bounded: a spent round raises before the first dial.
def test_a_spent_network_round_fails_before_dialing(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = replace(_config(tmp_path, served.url), fetch_round_s=0.0)

        with pytest.raises(TimeoutError, match="bounded network round"):
            copy_batch(pool, batch.request, config)

    assert served.pulls == []
    assert _copies(db_conn) == [(manifest.batch_id, False)]


# meiva:note
# The receiver holds no database connection across the network: with a one-connection pool,
# the gateway stand-in borrows that connection during every pull, and a receiver still holding
# it would starve the pull.
def test_the_database_connection_is_released_across_the_pull(
    database: Database, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first", b"second")
    manifest = batch.request.manifest
    with database.pool(min_size=1, max_size=1) as single:
        with serve_objects(
            agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents, pool=single
        ) as served:
            config = _config(tmp_path, served.url)
            copy_batch(single, batch.request, config)

        assert len(served.pulls) == 2
        assert batch.stored(config, 1).read_bytes() == b"second"


# meiva:note
# The ready write fails after the files were published: nothing is marked ready, and a retry
# verifies the files on disk without a second pull and marks the batch ready once.
def test_a_failed_ready_commit_recovers_without_a_second_copy(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        with (
            failing_write(
                db_conn,
                "upload_delivery_copies",
                when="NEW.ready_at IS NOT NULL",
                message="ready commit failure",
            ),
            pytest.raises(psycopg.errors.RaiseException, match="ready commit failure"),
        ):
            copy_batch(pool, batch.request, config)
        assert _copies(db_conn) == [(manifest.batch_id, False)]
        assert batch.stored(config, 0).read_bytes() == b"first"

        proof = copy_batch(pool, batch.request, config)

    assert len(served.pulls) == 1
    assert proof.directory == str(batch.stored(config, 0).parent)
    assert _copies(db_conn) == [(manifest.batch_id, True)]


# meiva:note
# Three receivers racing on one batch converge on one reservation row, one set of files and
# one proof.
def test_concurrent_receivers_converge_on_one_copy(
    pool: ConnectionPool, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first", b"second")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)

        def copy(_attempt: int) -> CopyProof:
            return copy_batch(pool, batch.request, config)

        with ThreadPoolExecutor(max_workers=3) as executor:
            proofs = list(executor.map(copy, range(3)))

    assert proofs[0] == proofs[1] == proofs[2]
    assert _copies(db_conn) == [(manifest.batch_id, True)]
    assert hashlib.sha256(batch.stored(config, 0).read_bytes()).hexdigest() == (
        manifest.objects[0].sha256
    )


# meiva:note
# An unconfigured gateway only matters when something has to be pulled: a batch whose files
# are all present proves itself without dialing, and an absent one fails loudly.
def test_a_blank_gateway_url_fails_only_when_a_pull_is_needed(
    pool: ConnectionPool, tmp_path: Path
) -> None:
    batch = _batch(tmp_path, b"first")
    manifest = batch.request.manifest
    with serve_objects(
        agent_id=_AGENT, batch_id=manifest.batch_id, objects=batch.contents
    ) as served:
        config = _config(tmp_path, served.url)
        copy_batch(pool, batch.request, config)
    blank = replace(config, access=replace(config.access, gateway_url=" "))

    assert copy_batch(pool, batch.request, blank).target == config.unit

    batch.stored(config, 0).unlink()
    with pytest.raises(RuntimeError, match="gateway_url unset"):
        copy_batch(pool, batch.request, blank)
