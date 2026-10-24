# meiva:note
# services/backup/dump.py — the shell around the plans, on real files and real tools.
#
# Every dump here is a real `pg_dump` of a real database piped through a real `openssl`,
# under a home of the test's own: retention on real files, publication, failure
# cleanup and the lock. The decisions themselves (names, argv, retention order, due-ness)
# are asserted by value in `test_dump_plan.py` and `test_schedule.py`.

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from psycopg.conninfo import conninfo_to_dict

from services.backup import dump as backup
from services.backup.artifact import names, passphrase
from services.backup.artifact.offsite_plan import OssTarget
from services.backup.artifact.tests.oss_fake import BUCKET, fake_oss
from services.backup.layout import BackupTree
from services.backup.tests.support import held_table_lock, minted_tree, scratch_database

_NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
_KEEP = 7


@pytest.fixture
def tree(tmp_path: Path) -> BackupTree:
    return minted_tree(tmp_path)


def _dbname(db_url: str) -> str:
    return str(conninfo_to_dict(db_url)["dbname"])


def _run(
    tree: BackupTree,
    db_url: str,
    now: datetime = _NOW,
    *,
    offsite_target: OssTarget | None = None,
    timeout_s: float = backup.DUMP_TIMEOUT_S,
) -> Path:
    return backup.run_backup(
        now,
        tree=tree,
        db_url=db_url,
        keep=_KEEP,
        offsite_target=offsite_target,
        timeout_s=timeout_s,
    )


# meiva:note
# The session database's dial, aimed at a port nothing listens on.
def _refused_url(db_url: str) -> str:
    parts = conninfo_to_dict(db_url)
    return f"postgresql://{parts['user']}@127.0.0.1:1/{parts['dbname']}"


def _touch(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_bytes(b"x")
    return p


# meiva:note
# A separate interpreter that takes the home's `backup_lock`, signals, and holds it.
def spawn_backup_lock_holder(home: Path, ready: Path, hold_s: float) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).with_name("child_backup_lock_holder.py")),
            str(ready),
            str(hold_s),
            str(home),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ),
    )


def await_backup_lock_holder(ready: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None, "the holder exited before taking the backup lock"
        assert time.monotonic() < deadline, "the holder never took the backup lock"
        time.sleep(0.02)


# meiva:note
# ─── retention and discovery on real files ───


# meiva:note
# DST-fold dumps order by their UTC instants, preserving both repeated wall times.
def test_prune_order_survives_the_dst_fold(tree: BackupTree) -> None:
    # meiva:note
    # 01:30 PDT
    pdt = _touch(tree.managed, "meiva-20261101T083000Z.dump")
    # meiva:note
    # 01:30 PST, one hour later
    pst = _touch(tree.managed, "meiva-20261101T093000Z.dump")
    assert [p for _ts, p in backup.managed_dumps(tree.managed)] == [pdt, pst]

    for i in range(2, _KEEP + 1):
        _touch(tree.managed, f"meiva-202611{i:02d}T090000Z.dump")

    # meiva:note
    # the earlier half of the fold
    assert backup.prune(tree.managed, keep=_KEEP) == [pdt]
    assert pst.exists() and not pdt.exists()


# meiva:note
# Hand-made dumps / stray files stay.
#
# That includes names no writer produces any more: the wall-clock stamp of the
# pre-cutover naming and the `pre-update` / `pitr-activation` kind segments.
def test_foreign_files_are_neither_counted_nor_pruned(tree: BackupTree) -> None:
    foreign = [
        _touch(tree.managed, "manual.dump"),
        _touch(tree.managed, "meiva-before-migration.dump"),
        _touch(tree.managed, "notes.txt"),
        _touch(tree.managed, "meiva-20260610-030001.dump"),
        _touch(tree.managed, "meiva-20260610T100001Z.pre-update.dump.enc"),
        _touch(
            tree.managed,
            "meiva-20260610T100001Z.pitr-activation-11111111-1111-1111-1111-111111111111.dump.enc",
        ),
    ]

    assert backup.managed_dumps(tree.managed) == []
    assert backup.prune(tree.managed, keep=_KEEP) == []
    assert all(p.exists() for p in foreign)


def test_prune_keeps_the_newest_and_deletes_the_rest(tree: BackupTree) -> None:
    # meiva:note
    # 10 days, oldest first
    pool = [f"meiva-202606{i:02d}T100000Z.dump" for i in range(1, 11)]
    for name in pool:
        _touch(tree.managed, name)

    removed = backup.prune(tree.managed, keep=_KEEP)

    assert [p.name for p in removed] == pool[: len(pool) - _KEEP]
    assert sorted(p.name for p in tree.managed.glob("*.dump")) == pool[len(pool) - _KEEP :]


def test_prune_keep_is_an_argument(tree: BackupTree) -> None:
    for day in range(1, 6):
        _touch(tree.managed, f"meiva-2026060{day}T100000Z.dump")

    assert len(backup.prune(tree.managed, keep=3)) == 2
    assert len(backup.managed_dumps(tree.managed)) == 3


# meiva:note
# ─── the real pipeline ───


# meiva:note
# Real pg_dump of the session's Postgres (an explicit dial: not a born home): the dump
# lands under the managed name, the .partial is gone, old dumps prune, and the artifact
# reverses to a restorable custom archive.
@pytest.mark.skipif(not backup.has_pg_tool("pg_dump"), reason="needs a native pg_dump binary")
def test_run_backup_real_dump_and_prune(tree: BackupTree, db_url: str) -> None:
    for i in range(1, _KEEP + 1):
        _touch(tree.managed, f"test-2026060{i}T100000Z.dump")

    path = _run(tree, db_url)

    assert path.parent == tree.managed
    assert names.DUMP_NAME_RE.match(path.name)
    assert path.stat().st_size > 0
    assert not list(tree.managed.glob("*.partial")) and not list(tree.managed.glob(".backup-key-*"))
    # meiva:note
    # `keep` pre-seeded + 1 new -> the oldest pre-seed pruned, `keep` remain.
    assert len(backup.managed_dumps(tree.managed)) == _KEEP
    assert not (tree.managed / "test-20260601T100000Z.dump").exists()
    # meiva:note
    # The fresh dump is restorable input: decrypt, then list its TOC directly.
    restored = tree.root / "listed.dump"
    backup.decrypt_artifact(path, restored, tree=tree)
    assert restored.read_bytes().startswith(b"PGDMP"), "decrypted artifact is not a custom dump"
    listing = subprocess.run(  # noqa: S603
        [str(backup.pg_tool("pg_restore")), "--list", str(restored)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    assert "checkpoints" in listing.stdout, "the recoverable dump must keep the checkpoint tables"


def test_the_artifact_name_is_utc_stamped_whatever_zone_the_instant_carries(
    tree: BackupTree, db_url: str
) -> None:
    shanghai = datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    path = _run(tree, db_url, shanghai)

    assert path.name == f"{_dbname(db_url)}-20260609T190000Z.dump.enc"


def test_a_same_second_dump_never_replaces_a_prior_one(tree: BackupTree, db_url: str) -> None:
    first = _run(tree, db_url)
    first_bytes = first.read_bytes()

    second = _run(tree, db_url)

    assert first.name == f"{_dbname(db_url)}-20260808T100000Z.dump.enc"
    assert second.name == f"{_dbname(db_url)}-20260808T100001Z.dump.enc"
    assert first.read_bytes() == first_bytes


# meiva:note
# The backup directory and completed dump are owner-only despite umask drift.
def test_run_backup_repairs_storage_permissions(tree: BackupTree, db_url: str) -> None:
    tree.managed.mkdir(parents=True)
    tree.managed.chmod(0o755)

    target = _run(tree, db_url)

    assert tree.managed.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600


def test_a_naive_instant_is_refused_before_anything_is_written(
    tree: BackupTree, db_url: str
) -> None:
    with pytest.raises(ValueError, match="TZ-aware"):
        _run(tree, db_url, datetime.fromisoformat("2026-06-10T03:00:00"))

    assert not list(tree.managed.glob("*"))


# meiva:note
# The dial refuses, so `pg_dump` exits 1 for real; its reaped writer's partial is removed,
# and a dead run's partial that no process holds open is swept.
def test_a_failed_dump_leaves_no_plaintext_and_the_next_run_sweeps_a_dead_ones(
    tree: BackupTree, db_url: str
) -> None:
    stale = _touch(tree.managed, "other-20260801T101500Z.dump.partial")

    with pytest.raises(RuntimeError, match="pg_dump exited 1"):
        _run(tree, _refused_url(db_url))

    assert not stale.exists()
    assert not list(tree.managed.glob("*.partial")) and not list(tree.managed.glob(".backup-key-*"))
    assert not list(tree.managed.glob("*.dump.enc"))


# meiva:note
# A bounded caller's deadline applies to the pipeline: the blocked child is killed, reaped
# and leaves no plaintext behind.
def test_the_requested_timeout_bounds_a_pg_dump_that_cannot_get_its_lock(
    tree: BackupTree, db_url: str
) -> None:
    with scratch_database(db_url) as url, held_table_lock(url):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _run(tree, url, timeout_s=1.5)

    assert time.monotonic() - started < 30
    assert not list(tree.managed.glob("*.partial")) and not list(tree.managed.glob(".backup-key-*"))
    assert not list(tree.managed.glob("*.dump.enc"))


def test_the_off_site_leg_publishes_the_finished_artifact_when_asked(
    tree: BackupTree, db_url: str
) -> None:
    credentials = tree.root / "oss.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text('{"access_key_id": "id", "access_key_secret": "secret"}')

    with fake_oss() as server:
        target = OssTarget(server.endpoint, BUCKET, credentials)
        local_only = _run(tree, db_url)
        assert server.files == {}, "no off-site target means no network publish"
        published = _run(tree, db_url, offsite_target=target)

    assert local_only.exists() and published.exists()
    [key] = server.files
    assert key == f"{names.REMOTE_ROOT}/{published.name}"
    assert server.files[key]["data"] == published.read_bytes()


# meiva:note
# ─── decryption ───


def test_a_failed_decrypt_reports_the_exit_and_removes_the_private_key(tree: BackupTree) -> None:
    artifact = tree.root / "artifact.dump.enc"
    artifact.write_bytes(b"not an encrypted dump at all")
    output = tree.root / "backup.dump"

    with pytest.raises(RuntimeError, match=r"^backup decrypt exited [1-9]\d*$"):
        backup.decrypt_artifact(artifact, output, tree=tree)

    assert not list(tree.root.glob(".backup-key-*"))
    assert output.stat().st_mode & 0o777 == 0o600


# meiva:note
# The published artifact reverses with the pinned passphrase as `db-restore.md` reads it:
# this fails if the key-file derivation, cipher invocation or name changes incompatibly.
def test_the_passphrase_an_operator_reads_decrypts_the_artifact(
    tree: BackupTree, db_url: str
) -> None:
    artifact = _run(tree, db_url)
    key_file = tree.root / "decrypt.key"
    key_file.write_text(passphrase.resolve(tree.home), encoding="utf-8")
    key_file.chmod(0o600)
    custom_dump = tree.root / "restored.dump"

    decrypted = subprocess.run(  # noqa: S603
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-kfile",
            str(key_file),
            "-in",
            str(artifact),
            "-out",
            str(custom_dump),
        ],
        capture_output=True,
        check=False,
    )

    assert decrypted.returncode == 0, decrypted.stderr.decode()
    assert custom_dump.read_bytes().startswith(b"PGDMP")


# meiva:note
# ─── the composition line ───


# meiva:note
# The composition query itself is pinned against a real throwaway DB: a fresh DB with no
# checkpoint tables reads 0 instead of failing (the to_regclass path).
def test_db_size_breakdown_real_db(db_url: str) -> None:
    with scratch_database(db_url) as url:
        line = backup.db_size_breakdown(url)

    assert line.startswith("db=") and "checkpoint=" in line and "rest=" in line
    assert "checkpoint=0MiB" in line


# meiva:note
# Best-effort: a failed sample never fails the backup.
def test_a_database_that_cannot_be_dialed_degrades_to_unavailable(db_url: str) -> None:
    assert backup.db_size_breakdown(_refused_url(db_url)) == "unavailable"


@pytest.mark.parametrize("root", ["", "/abs", "trailing/"])
def test_publish_offsite_entry_rejects_a_malformed_root(
    root: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = tmp_path / "meiva-20260916T030000Z.dump.enc"
    artifact.write_bytes(b"encrypted")

    with pytest.raises(SystemExit) as raised:
        backup.run_offsite_publisher(["--publish-offsite", str(artifact), "--offsite-root", root])

    assert raised.value.code == 2
    assert "--offsite-root" in capsys.readouterr().err
