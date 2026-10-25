"""Hard process death and database-disconnected file writers, using real Postgres."""

import json
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.paths.private_dir import ensure_private_dir
from gateway.routers.upload import batches as upload_batches
from gateway.routers.upload.batches import UploadItem
from gateway.schemas.uploads import UploadedBatch

_CHILD = Path(__file__).with_name("child_batch_writer.py")


_ITEMS: list[UploadItem] = [("a.png", b"aaa", "image/png"), ("b.png", b"bbb", "image/png")]
_NAMES = ["a.png", "b.png"]


def _save(pool: ConnectionPool, directory: Path, *, local_machine: str) -> UploadedBatch:
    return upload_batches.save_keyed_batch(
        pool,
        "crash",
        123,
        directory,
        _ITEMS,
        _NAMES,
        max_bytes=6,
        max_files=2,
        local_machine=local_machine,
    )


@pytest.fixture
def batch_target(db_conn: psycopg.Connection, tmp_path: Path) -> Path:
    db_conn.execute("INSERT INTO agents (id) VALUES (123)")
    db_conn.execute("INSERT INTO agents_meta (id, status) VALUES (123, 'idling')")
    db_conn.commit()
    ensure_private_dir(tmp_path / "writer-home")
    return ensure_private_dir(tmp_path / "Downloads" / "MeivaAgent-123")


@pytest.mark.parametrize("phase", ["receiving", "first-file", "published", "ready"])
def test_hard_death_recovers_fixed_objects(
    db_conn: psycopg.Connection,
    batch_target: Path,
    phase: str,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    child = subprocess.run(  # noqa: S603 -- fixed Python program against isolated test DB
        [sys.executable, str(_CHILD)],
        input=json.dumps(
            {
                "dsn": db_conn.info.dsn,
                "directory": str(batch_target),
                "phase": phase,
                "home": str(batch_target.parent.parent / "writer-home"),
                "machine": machine_identity.name(),
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 41, child.stderr
    row = db_conn.execute("SELECT manifest, receipt FROM agent_upload_batches").fetchone()
    assert row is not None
    names = [item["stored_name"] for item in row[0]]
    assert (row[1] is not None) == (phase == "ready")
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn, min_size=1, max_size=2) as pool:
        receipt = _save(pool, batch_target, local_machine=machine_identity.name())
        assert _save(pool, batch_target, local_machine=machine_identity.name()) == receipt
    assert [Path(file.path).name for file in receipt.files] == names
    assert [Path(file.path).read_bytes() for file in receipt.files] == [b"aaa", b"bbb"]
    assert len([p for p in batch_target.iterdir() if p.is_file()]) == 2


# meiva:note
# A writer that outlives its database connection holds its manifest and keeps publishing.
#
# Another writer recovers the batch first. The old one then links the same objects: create-only
# publication verifies the finals already there and leaves every one of them in place.
def test_disconnected_writer_can_finish_after_recovery_without_overwriting(
    db_conn: psycopg.Connection, batch_target: Path, *, machine_identity: MachineIdentityReader
) -> None:
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn, min_size=1, max_size=3) as pool:
        old_writer = upload_batches.claim_batch(
            pool,
            "crash",
            123,
            batch_target,
            _ITEMS,
            _NAMES,
            max_bytes=6,
            max_files=2,
            local_machine=machine_identity.name(),
        )
        recovered = _save(pool, batch_target, local_machine=machine_identity.name())
        finals = {
            path.name: path.stat().st_ino for path in batch_target.iterdir() if path.is_file()
        }
        upload_batches.publish_files(batch_target, old_writer.manifest, _ITEMS)
        assert _save(pool, batch_target, local_machine=machine_identity.name()) == recovered
    assert {
        path.name: path.stat().st_ino for path in batch_target.iterdir() if path.is_file()
    } == finals
    assert [Path(file.path).read_bytes() for file in recovered.files] == [b"aaa", b"bbb"]
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 1


def test_independent_processes_share_quota(
    db_conn: psycopg.Connection, batch_target: Path, *, machine_identity: MachineIdentityReader
) -> None:
    children: list[subprocess.Popen[str]] = []
    try:
        for key in ("one", "two"):
            child = subprocess.Popen(  # noqa: S603 -- fixed program against isolated test DB
                [sys.executable, str(_CHILD)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdin is not None
            child.stdin.write(
                json.dumps(
                    {
                        "dsn": db_conn.info.dsn,
                        "directory": str(batch_target),
                        "phase": "ready",
                        "key": key,
                        "home": str(batch_target.parent.parent / "writer-home"),
                        "machine": machine_identity.name(),
                    }
                )
            )
            child.stdin.close()
            children.append(child)
        codes = [child.wait(timeout=30) for child in children]
        assert sorted(codes) == [1, 41]
        loser = next(child for child in children if child.returncode == 1)
        assert loser.stderr is not None
        assert "413" in loser.stderr.read()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 1


def test_receipts_use_explicit_writable_transactions(
    db_conn: psycopg.Connection, batch_target: Path, *, machine_identity: MachineIdentityReader
) -> None:
    with ConnectionPool[psycopg.Connection](
        db_conn.info.dsn,
        min_size=1,
        max_size=1,
        kwargs={"options": "-c default_transaction_read_only=on"},
    ) as pool:
        assert len(_save(pool, batch_target, local_machine=machine_identity.name()).files) == 2
