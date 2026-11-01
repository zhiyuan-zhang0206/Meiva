"""Restore one encrypted database artifact into throwaway Postgres and verify it.

The default target is the newest managed artifact in the local backup directory;
pass an explicit artifact path to exercise a different retained copy. The drill
never touches the live database. It decrypts, restores into a native
throwaway Postgres cluster, verifies the recovery-source tables and a
checkpoint reader sample, then removes every scratch file and the throwaway
cluster.
"""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import ormsgpack
from psycopg import Connection

from base.agents.history import checkpoint as checkpoint_reader
from base.agents.history.checkpoint_serde import (
    READER_CHECKPOINT_MSGPACK_TYPES,
    RETIRED_CHECKPOINT_MSGPACK_TYPES,
    STATIC_CHECKPOINT_MSGPACK_TYPES,
    RetiredCheckpointType,
    checkpoint_type_status,
    observed_checkpoint_types,
)
from base.agents.history.checkpoint_serializer import CheckpointSerializer
from base.cluster.dataplane.pg_throwaway_base import (
    ThrowawayConfig,
    configured_base,
    format_bytes,
    select_throwaway_base,
)
from base.cluster.dataplane.pg_tools import has_pg_tool as has_pg_tool
from base.cluster.dataplane.pg_tools import pg_tool, throwaway_postgres
from base.config import ConfigBoot
from base.db import Database, connect_url
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.db_authority.model import GATEWAY_GROUP, RUNNER_GROUP
from base.log import logger
from base.native_process.child_env import inherited_process_env
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.delivery.state import process_name
from services.backup import dump as backup
from services.backup.dump_lines import restore_failure_message
from services.backup.layout import BackupTree


@dataclass(frozen=True)
class RestoreReport:
    """Observed data-plane facts from one successful scratch restore."""

    agents: int
    checkpoint_blobs: int
    checkpoints: int
    checkpoint_writes: int
    sample_agent_id: int
    sample_message_count: int
    agents_owner: str
    census: TypeCensus


# meiva:note
# The newest managed artifact of the tree's dump pool.
def newest_artifact(tree: BackupTree) -> Path:
    artifacts = backup.managed_dumps(tree.managed)
    if not artifacts:
        raise RuntimeError("no managed backup artifact exists")
    return artifacts[-1][1]


RESTORE_ROLES = ("meiva_main", GATEWAY_GROUP, RUNNER_GROUP, "grafana_ro", "zzy")
"""Roles a managed dump's OWNER/GRANT statements reference. initdb only
creates the `meiva` superuser; without these pg_restore fails on
`role "..." does not exist` (2026-08-27 prod drill finding). They are created
as plain LOGIN roles without a password (trust auth): a restore needs them
only to exist. The two write-generation capability groups
(`base.cluster.authority`) hold every application grant and default
privilege, so a dump of an always-authenticated home names both; the
generation logins own nothing and hold no direct grant, so none is needed.
`zzy` is the live admin role ad-hoc artifacts are created under (the
`model_sweep_backup_*` sweep convention); the dump re-owns such objects to it,
so the scratch cluster must carry the role too (2026-09-21 prod drill finding)."""


def _ensure_restore_roles(db_url: str) -> None:
    """Create the dump-referenced roles in the throwaway cluster, idempotently."""
    from psycopg import sql as pgsql

    with connect_url(db_url, autocommit=True, unbounded=True) as conn:
        for role in RESTORE_ROLES:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if exists is None:
                conn.execute(pgsql.SQL("CREATE ROLE {} LOGIN").format(pgsql.Identifier(role)))


# meiva:note
# Load the custom dump into the disposable target database.
def restore_dump(
    raw_dump: Path, db_url: str, *, base: Path, home: Path, env: Mapping[str, str]
) -> None:
    proc = subprocess.run(  # noqa: S603
        [
            str(pg_tool("pg_restore", home=home)),
            "--clean",
            "--if-exists",
            "--dbname",
            db_url,
            str(raw_dump),
        ],
        capture_output=True,
        check=False,
        timeout=backup.DUMP_TIMEOUT_S,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            restore_failure_message(
                proc.returncode, proc.stderr, base, format_bytes(shutil.disk_usage(base).free)
            )
        )


# meiva:note
# The type census: the part of "the data is readable" that the checks below cannot reach. A
# required table, a row count and one conversation all survive a checkpoint holding a class
# this build no longer has — the serializer answers such an envelope with the raw dict and a
# warning, so the drill would stay green while the history silently degrades. The census reads
# the type names out of the newest checkpoint of the newest conversations and accounts for
# every one of them.
# meiva:note
# Conversations the census samples, newest first. A whole-store census is out of reach: on
# 2026-10-30 `checkpoints` held 5.28M rows over 17 GB, `checkpoint_writes` 11.6M rows over
# 11 GB and `checkpoint_blobs` 7.7 GB, so decoding all of it costs more than the restore it
# verifies. The 200 newest conversations spanned back nine days — the population a reader
# actually opens.
CENSUS_THREADS = 200

# meiva:note
# The newest conversations by conversation time. Not by `checkpoint_id`: that ordering is
# textual, the store mixes v6 and v7 ids, and rows the test suite left behind sort above the
# real ones — the same reason the message sample orders by `ts`.
_CENSUS_THREADS = """
SELECT thread_id
FROM checkpoints
WHERE checkpoint_ns = ''
GROUP BY thread_id
ORDER BY max((checkpoint->>'ts')::timestamptz) DESC NULLS LAST
LIMIT %s
"""

# meiva:note
# The thread's newest checkpoint and, one row per channel, the version it carries.
# `jsonb_each_text` (not the jsonb object) because channel names and versions cross into
# Python as text, and the census only ever compares them as opaque keys.
_CENSUS_LATEST_CHECKPOINT = """
WITH latest AS (
    SELECT checkpoint_id,
           (checkpoint->>'ts')::timestamptz AS ts,
           checkpoint->'channel_versions' AS versions
    FROM checkpoints
    WHERE thread_id = %s AND checkpoint_ns = ''
    ORDER BY (checkpoint->>'ts')::timestamptz DESC NULLS LAST
    LIMIT 1
)
SELECT latest.checkpoint_id, latest.ts, channels.key, channels.value
FROM latest
LEFT JOIN LATERAL jsonb_each_text(latest.versions) AS channels ON TRUE
"""

# meiva:note
# The materialized value of every channel this checkpoint still carries. A delta channel
# (`messages` since the #3180 cutover) keeps no materialized blob, so it adds no row here; its
# newest objects arrive as writes below.
_CENSUS_BLOBS = """
SELECT b.channel, b.version, b.type, b.blob
FROM unnest(%s::text[], %s::text[]) AS wanted(channel, version)
JOIN checkpoint_blobs b
  ON b.thread_id = %s
 AND b.checkpoint_ns = %s
 AND b.channel = wanted.channel
 AND b.version = wanted.version
"""

# meiva:note
# What this checkpoint itself wrote: the newest superstep, message objects included.
_CENSUS_WRITES = """
SELECT channel, type, blob
FROM checkpoint_writes
WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
"""


# meiva:note
# Which stored classes one drill looked at, and what it could account for. Every field is a
# denominator or a name: a green run quotes them, so a reader of the record can tell what the
# green covered. `other_forms` counts rows whose `type` column is not msgpack (null, bytes,
# json, pickle): copied by the restore, not decoded by the census.
@dataclass(frozen=True)
class TypeCensus:
    threads: int
    blobs: int
    writes: int
    other_forms: int
    scanned_bytes: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    types: tuple[str, ...]
    retired: tuple[str, ...]
    revived: tuple[str, ...]
    unknown: tuple[str, ...]
    undecodable: tuple[str, ...]

    # meiva:note
    # Why this census does not prove the data readable, or None when it does: a zero
    # denominator, a row that does not decode, a name nothing accounts for, or a retired class
    # this build still resolves.
    def failure(self) -> str | None:
        if self.threads == 0:
            return "the restored store holds no checkpoint to census"
        if self.blobs + self.writes == 0:
            return f"the {self.threads} sampled conversations exposed no checkpoint row to read"
        if self.undecodable:
            return "checkpoint rows this build cannot decode: " + "; ".join(self.undecodable[:5])
        if self.unknown:
            return (
                "checkpoint rows name classes this build can neither load nor account for: "
                + ", ".join(self.unknown)
                + " — either the class exists and belongs in the allowlist, or its writing"
                " code is gone and belongs in the retired registry"
            )
        if self.revived:
            return (
                "classes the retired registry lists are importable in this build: "
                + ", ".join(self.revived)
                + " — either the class came back (un-retire it, and let the allowlist name it)"
                " or the entry is stale"
            )
        return None

    # meiva:note
    # One line for the drill record: the denominators a green run rests on.
    def summary(self) -> str:
        window = "no window"
        if self.oldest_ts is not None and self.newest_ts is not None:
            window = f"{self.oldest_ts:%Y-%m-%d}…{self.newest_ts:%Y-%m-%d}"
        copied = f", {self.other_forms} non-msgpack" if self.other_forms else ""
        return (
            f"type census: {len(self.types)} types over {self.threads} conversations"
            f" ({window}), {self.blobs} blobs + {self.writes} writes"
            f" = {format_bytes(self.scanned_bytes)}{copied}, {len(self.unknown)} unknown,"
            f" {len(self.retired)} retired ({len(self.revived)} revived)"
        )


# meiva:note
# The census as the rows stream by; `census_checkpoint_types` freezes it into `TypeCensus`.
@dataclass
class _Tally:
    threads: int = 0
    blobs: int = 0
    writes: int = 0
    other_forms: int = 0
    scanned_bytes: int = 0
    oldest_ts: datetime | None = None
    newest_ts: datetime | None = None
    registry: tuple[RetiredCheckpointType, ...] = RETIRED_CHECKPOINT_MSGPACK_TYPES
    observed: set[tuple[str, str]] = field(default_factory=set)
    retired: set[tuple[str, str]] = field(default_factory=set)
    retired_rows: dict[tuple[str, str], bytes] = field(default_factory=dict)
    undecodable: list[str] = field(default_factory=list)

    def conversation(self, written_at: datetime | None) -> None:
        self.threads += 1
        if written_at is None:
            return
        if self.oldest_ts is None or written_at < self.oldest_ts:
            self.oldest_ts = written_at
        if self.newest_ts is None or written_at > self.newest_ts:
            self.newest_ts = written_at

    # meiva:note
    # Account for one stored row: decode its type names, and keep the bytes of the first row
    # that names a retired type so the revival probe can decode exactly that row later.
    def row(
        self,
        blob_type: str | None,
        blob: bytes | memoryview | None,
        *,
        where: str,
    ) -> None:
        if blob is None:
            return
        self.scanned_bytes += len(blob)
        if blob_type != "msgpack":
            self.other_forms += 1
            return
        raw = bytes(blob)
        try:
            named = observed_checkpoint_types(raw)
        except ormsgpack.MsgpackDecodeError as exc:
            self.undecodable.append(f"{where}: {exc}")
            return
        for key in named:
            self.observed.add(key)
            if _entry_for(key, self.registry) is not None:
                self.retired.add(key)
                self.retired_rows.setdefault(key, raw)


# meiva:note
# The registry entry for `key` in the registry this run was given.
def _entry_for(
    key: tuple[str, str], registry: tuple[RetiredCheckpointType, ...]
) -> RetiredCheckpointType | None:
    for entry in registry:
        if entry.key == key:
            return entry
    return None


def _rendered(keys: set[tuple[str, str]] | frozenset[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(sorted(f"{module}.{name}" for module, name in keys))


# meiva:note
# The one retirement fault the drill can prove without reading history: the reader of this
# build still resolves a class the registry lists as deleted. A stored write dated after the
# deletion cannot tell a regression from a host that still runs pre-deletion code, and this
# cluster's deploys are user-gated and lag by weeks (2026-10-30: five of five newest
# conversations carried a `NativeWorkTarget` version first written 2026-10-21 04:0x UTC, nine
# hours after the deleting commit landed 2026-10-20 11:52 UTC). So `retired_at` stays context,
# never a verdict: a clock date describes when the fix landed, not which build wrote the row.
#
# The verdict is pinned to the reader itself, not to `importlib`: the stored row is decoded
# with the same serializer the reader uses, and a difference against a widened allowlist means
# the class is in this tree and the stored arguments still build it. A name back in the
# reader's allowlist counts too — the registry is contradicting the reader either way.
def _revived(
    retired_rows: Mapping[tuple[str, str], bytes],
    *,
    reader: CheckpointSerializer,
    allowed: frozenset[tuple[str, str]] = READER_CHECKPOINT_MSGPACK_TYPES,
) -> set[tuple[str, str]]:
    revived: set[tuple[str, str]] = set()
    for key, blob in retired_rows.items():
        if key in allowed or _builds_with(reader, key, blob):
            revived.add(key)
    return revived


# meiva:note
# Whether a reader that was allowed to load `key` builds it from the stored row. A blocked
# envelope comes back as its raw payload, so decoding the same row twice — once with the
# reader's allowlist, once with `key` added to it — differs exactly when the class is in this
# tree and the stored arguments still build it. A row that does not decode at all is already
# recorded by `_Tally.row`, so a decode failure here answers "not built".
def _builds_with(reader: CheckpointSerializer, key: tuple[str, str], blob: bytes) -> bool:
    widened = CheckpointSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES | {key})
    try:
        return widened.loads_typed(("msgpack", blob)) != reader.loads_typed(("msgpack", blob))
    except (ImportError, AttributeError, TypeError, ValueError, ormsgpack.MsgpackDecodeError):
        return False


# meiva:note
# Read the type names of the newest stored conversation per sampled thread — its materialized
# channels plus the writes of that checkpoint — and account for every one of them.
def census_checkpoint_types(
    conn: Connection[Any],
    *,
    threads: int = CENSUS_THREADS,
    retired: tuple[RetiredCheckpointType, ...] = RETIRED_CHECKPOINT_MSGPACK_TYPES,
) -> TypeCensus:
    tally = _Tally(registry=retired)
    sampled = conn.execute(_CENSUS_THREADS, (threads,)).fetchall()
    for (thread_id_value,) in sampled:
        thread_id = str(thread_id_value)
        rows = conn.execute(_CENSUS_LATEST_CHECKPOINT, (thread_id,)).fetchall()
        if not rows:
            continue
        checkpoint_id, written_at = rows[0][0], rows[0][1]
        tally.conversation(written_at)
        wanted = [
            (str(channel), str(version)) for _, _, channel, version in rows if channel is not None
        ]
        if wanted:
            blob_rows = conn.execute(
                _CENSUS_BLOBS,
                (
                    [channel for channel, _ in wanted],
                    [version for _, version in wanted],
                    thread_id,
                    "",
                ),
            ).fetchall()
            tally.blobs += len(blob_rows)
            for channel, _version, blob_type, blob in blob_rows:
                tally.row(blob_type, blob, where=f"thread {thread_id} channel {channel}")
        write_rows = conn.execute(_CENSUS_WRITES, (thread_id, "", checkpoint_id)).fetchall()
        tally.writes += len(write_rows)
        for channel, blob_type, blob in write_rows:
            tally.row(blob_type, blob, where=f"thread {thread_id} channel {channel}")
    unknown = {
        key
        for key in tally.observed
        if checkpoint_type_status(key, retired=retired) not in {"allowed", "retired"}
    }
    revived = _revived(
        tally.retired_rows,
        reader=CheckpointSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES),
    )
    return TypeCensus(
        threads=tally.threads,
        blobs=tally.blobs,
        writes=tally.writes,
        other_forms=tally.other_forms,
        scanned_bytes=tally.scanned_bytes,
        oldest_ts=tally.oldest_ts,
        newest_ts=tally.newest_ts,
        types=_rendered(tally.observed),
        retired=_rendered(tally.retired),
        revived=_rendered(revived),
        unknown=_rendered(unknown),
        undecodable=tuple(tally.undecodable),
    )


def verify_restored_database(
    db_url: str, *, database_for_url: Callable[[str], Database]
) -> RestoreReport:
    """Verify schema, table counts, and a readable checkpoint conversation.

    The sample thread is the time-newest one (`checkpoint->>'ts'`): ordering
    by `checkpoint_id` is textual, and non-UUID test rows sort above real
    UUIDs, which picked a May test residue over a live conversation."""

    required_tables = ("agents", "checkpoint_blobs", "checkpoints", "checkpoint_writes")
    with connect_url(db_url, autocommit=True, unbounded=True) as conn:
        for table in required_tables:
            row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            if row is None or row[0] != table:
                raise RuntimeError(f"restored schema is missing {table}")
        agents = conn.execute("SELECT count(*) FROM agents").fetchone()
        blobs = conn.execute("SELECT count(*) FROM checkpoint_blobs").fetchone()
        checkpoints = conn.execute("SELECT count(*) FROM checkpoints").fetchone()
        writes = conn.execute("SELECT count(*) FROM checkpoint_writes").fetchone()
        sample = conn.execute(
            "SELECT thread_id FROM checkpoints ORDER BY checkpoint->>'ts' DESC NULLS LAST LIMIT 1"
        ).fetchone()
        owner = conn.execute(
            "SELECT pg_catalog.pg_get_userbyid(relowner) FROM pg_catalog.pg_class "
            "WHERE relname = 'agents' AND relnamespace = to_regnamespace('public')"
        ).fetchone()
        census = census_checkpoint_types(conn)
    if agents is None or blobs is None or checkpoints is None or writes is None:
        raise RuntimeError("restored count query returned no row")
    if sample is None or not str(sample[0]).isdigit():
        raise RuntimeError("restored checkpoints contain no readable agent conversation")

    sample_agent_id = int(sample[0])
    restored = database_for_url(db_url)
    messages = checkpoint_reader.load_checkpoint_messages_full(restored, sample_agent_id)
    if not messages:
        raise RuntimeError("restored checkpoint conversation has no messages")

    if owner is None or not owner[0]:
        raise RuntimeError("restored agents table has no resolvable owner")
    refusal = census.failure()
    if refusal is not None:
        raise RuntimeError(f"restored checkpoints do not read back: {refusal}")

    return RestoreReport(
        agents=agents[0],
        checkpoint_blobs=blobs[0],
        checkpoints=checkpoints[0],
        checkpoint_writes=writes[0],
        sample_agent_id=sample_agent_id,
        sample_message_count=len(messages),
        agents_owner=str(owner[0]),
        census=census,
    )


_SCRATCH_SPACE_FACTOR = 3.0
"""Scratch space the drill reserves on the base holding the restored copy, as a
multiple of the (decrypted) dump's size. A floor for picking the base, not a size
prediction: the restore holds the dump's content decompressed, and in-dump zstd
ratios vary with content mix. Measured on the daily drill: 2026-09-14 restored
>=17 GiB from a 10.16 GiB artifact (~1.7x); 2026-09-19 restored ~8 GiB from a
~3.0 GiB artifact (>=2.7x at 60s sampling — it outgrew the 7.8 GiB tmpfs that 2x
had cleared, task #4033). 3x keeps margin above the observed maximum while
remaining a floor. A base that clears it is not guaranteed to fit; one that fails
it is almost certainly too small."""


def scratch_space_requirement(raw_dump: Path) -> int:
    """Bytes the base holding this dump's restore must offer (an estimate — see
    `_SCRATCH_SPACE_FACTOR`)."""
    return int(raw_dump.stat().st_size * _SCRATCH_SPACE_FACTOR)


def run_drill(
    artifact: Path,
    *,
    tree: BackupTree,
    database_for_url: Callable[[str], Database],
    throwaway: ThrowawayConfig,
    foreground: bool = False,
    scratch_root: Path | None = None,
    env: Mapping[str, str],
) -> tuple[RestoreReport, float]:
    """Run the complete decrypt, restore, and verification drill.

    `scratch_root` places the decrypted dump inside a caller-owned private
    directory, so a caller that proves the drill's closure can remove it even
    when the drill was killed before its own cleanup.
    """
    if not artifact.is_file():
        raise RuntimeError(f"backup artifact does not exist: {artifact.name}")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="meiva-restore-drill-", dir=scratch_root) as tmp:
        scratch = Path(tmp)
        raw_dump = scratch / "backup.dump"
        backup.decrypt_artifact(artifact, raw_dump, tree=tree, env=env)
        required = scratch_space_requirement(raw_dump)
        base = select_throwaway_base(required, override=throwaway.override)
        logger.info(
            f"restore drill: scratch cluster on {base} "
            f"(estimate {format_bytes(required)} for artifact {artifact.name})"
        )
        with throwaway_postgres(
            config=throwaway, base=base, foreground=foreground
        ) as scratch_db_url:
            _ensure_restore_roles(scratch_db_url)
            restore_dump(raw_dump, scratch_db_url, base=base, home=tree.home, env=env)
            report = verify_restored_database(scratch_db_url, database_for_url=database_for_url)
    return report, time.monotonic() - started


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Restore and verify an encrypted Meiva DB backup.")
    parser.add_argument("artifact", nargs="?", type=Path, help="managed .dump.enc artifact")
    args = parser.parse_args(argv)
    config = ConfigBoot()
    config.read_process_environment()
    home = meiva_home()
    tree = BackupTree(home)
    throwaway = ThrowawayConfig(
        configured_base(config.view.data_plane.pg_throwaway_base),
        home=home,
        environ=config.process_environment(),
    )
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process=process_name())

    def database_for_url(url: str) -> Database:
        return Database(dataclasses.replace(db_config_from_boot(config), db_url=url), gate=gate)

    report, elapsed = run_drill(
        args.artifact or newest_artifact(tree),
        tree=tree,
        throwaway=throwaway,
        database_for_url=database_for_url,
        env=inherited_process_env(),
    )
    print(  # noqa: T201 — the operator command's result line, read on stdout
        "restore drill passed: "
        f"agents={report.agents} checkpoints={report.checkpoints} "
        f"checkpoint_blobs={report.checkpoint_blobs} "
        f"checkpoint_writes={report.checkpoint_writes} "
        f"sample_agent={report.sample_agent_id} messages={report.sample_message_count} "
        f"agents_owner={report.agents_owner} {report.census.summary()} "
        f"elapsed_seconds={elapsed:.1f}"
    )


if __name__ == "__main__":
    main()
