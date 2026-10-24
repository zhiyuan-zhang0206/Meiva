# meiva:note
# What one logical dump runs and keeps, as values in and values out.
#
# `services.backup.dump` owns the processes, the lock and the files. This module owns the
# decisions that need none of them: which names a dump and its two intermediates take,
# the exact argv of each tool, the one environment variable a dump child gets, and
# which managed dumps retention removes.

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.backup.artifact.names import TS_FORMAT

__all__ = [
    "DumpFiles",
    "decrypt_argv",
    "encrypt_argv",
    "pg_dump_argv",
    "pg_dump_env",
    "prune_victims",
    "target_files",
]

_TARGET_NAME_ATTEMPTS = 60


# meiva:note
# The three paths of one dump: the published artifact and the two private
# intermediates that exist only while the pipeline runs.
@dataclass(frozen=True)
class DumpFiles:
    target: Path
    dump_partial: Path
    encrypted_partial: Path


# meiva:note
# An unused managed name in `directory` for a dump of `dbname` taken at `now`, plus its
# intermediates. A name already in `taken` moves the stamp one second later, so a dump
# never replaces a prior one; sixty taken seconds in a row is a fault, not a retry.
def target_files(directory: Path, dbname: str, now: datetime, taken: Collection[str]) -> DumpFiles:
    for offset_s in range(_TARGET_NAME_ATTEMPTS):
        stamp = (now + timedelta(seconds=offset_s)).astimezone(UTC).strftime(TS_FORMAT)
        name = f"{dbname}-{stamp}.dump.enc"
        if name not in taken:
            stem = name.removesuffix(".dump.enc")
            return DumpFiles(
                target=directory / name,
                dump_partial=directory / f"{stem}.dump.partial",
                encrypted_partial=directory / f"{name}.partial",
            )
    raise RuntimeError("could not choose a distinct backup filename within 60 seconds")


# meiva:note
# The `pg_dump` argv. The archive compresses in-dump (zstd, PG 17+): a second external
# pass over the already-compressed archive gained under one percent for 13 to 47 s on
# the production database. No table is excluded: the LangGraph checkpoint tables are the
# only copy of conversation history. The password is never part of argv.
def pg_dump_argv(pg_dump: str, dump_partial: Path, conninfo: str) -> list[str]:
    return [
        pg_dump,
        "--format=custom",
        "--compress=zstd:3",
        "--file",
        str(dump_partial),
        "--dbname",
        conninfo,
    ]


# meiva:note
# The only environment a dump child gets. In particular a shell's PGPASSWORD is never
# inherited: a dump must not authenticate with another cluster's value.
def pg_dump_env(password: str) -> dict[str, str]:
    return {"PGPASSWORD": password} if password else {}


# meiva:note
# The `openssl enc` argv that encrypts a plaintext dump under the key file.
def encrypt_argv(key_file: Path, source: Path, out: Path) -> list[str]:
    return [
        "openssl",
        "enc",
        "-aes-256-cbc",
        "-pbkdf2",
        "-salt",
        "-kfile",
        str(key_file),
        "-in",
        str(source),
        "-out",
        str(out),
    ]


# meiva:note
# The `openssl enc -d` argv that reverses `encrypt_argv`.
def decrypt_argv(key_file: Path, artifact: Path, out: Path) -> list[str]:
    return [
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
        str(out),
    ]


# meiva:note
# The dumps retention removes: everything but the newest `keep`. `dumps` are the
# managed dumps keyed by their UTC instant, in any order; ties and the DST fold are
# ordered by instant, never by wall time.
def prune_victims(dumps: Sequence[tuple[datetime, Path]], keep: int) -> list[Path]:
    if keep < 1:
        raise ValueError(f"backup retention must keep at least one dump, got {keep}")
    ordered = sorted(dumps)
    return [path for _instant, path in ordered[: max(len(ordered) - keep, 0)]]
