"""Read-only memory search backend reconciliation — queries, two backends, diff.

The pilot tool for the backend switch: run the SAME query texts through
two backends and compare their path orderings. Sample queries come from
the memory pool's frontmatter descriptions; each is embedded once and
searched on both sides. Both backends (numpy / pgvector) are exact, so
their orderings should agree; the diff makes any disagreement visible.

Runs on the gateway box: needs GEMINI_API_KEY (embedding) and both
backends' services up (memory_search daemon / cluster PG).
It opens both backends read-only by default: the indexer daemon's cold-start
reconcile is the only normal writer. `--allow-write` is an intentional
operator escape hatch that requires a second confirmation before connecting.

Usage:
    .venv/bin/python -m services.derived.memory_indexer.memory_search_reconcile --a numpy --b pgvector --limit 50 --k 10
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader, MachineRoles
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.note_files import walk_notes
from base.packages.plugin_providers import build_model_catalog
from base.paths import gateway_memory_dir, meiva_home
from base.telemetry.delivery.state import process_name
from services.derived.memory_indexer.backends.base import MemorySearchBackend
from services.derived.memory_indexer.backends.factory import get_backend
from services.derived.memory_indexer.config import MemoryIndexerInputs
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError, EmbeddingProvider
from services.derived.memory_indexer.embeddings.factory import get_provider
from services.derived.memory_indexer.reconcile_plan import (
    confirm_write,
    connection_refusal_lines,
    ordering_lines,
    refusal_message,
    row_gap_lines,
    summary_line,
)


# meiva:note
# The command line, parsed: the two backends to compare, how many sample queries and how deep a
# top-k, and whether writable connections were asked for.
@dataclass(frozen=True)
class ReconcileArguments:
    a: str
    b: str
    limit: int
    k: int
    allow_write: bool


def sample_queries(limit: int, *, roles: MachineRoles, home: Path) -> list[str]:
    """Frontmatter descriptions from the memory pool, newest-first, capped at
    `limit` — the short entity-bearing lines the index exists to find."""
    queries: list[str] = []
    for _, note in walk_notes(gateway_memory_dir(roles=roles, home=home)):
        if note.description and note.description.strip():
            queries.append(note.description.strip())
        if len(queries) >= limit:
            break
    return queries


# meiva:note
# Ask on the terminal for the exact word before writable connects; the refusal text, or None.
def _confirmation_refusal() -> str | None:
    interactive = sys.stdin.isatty()
    answer: str | None = None
    if interactive:
        try:
            answer = input("This may rebuild backend storage. Type exact 'yes' to continue: ")
        except EOFError:
            answer = None
    return refusal_message(confirm_write(interactive=interactive, answer=answer))


def _connect_backends(
    backend_a: MemorySearchBackend,
    backend_b: MemorySearchBackend,
    *,
    readonly: bool,
    err: TextIO,
) -> bool:
    """Connect both backends and report a safe read-only schema refusal."""
    try:
        backend_a.connect()
        backend_b.connect()
    except Exception as exc:
        backend_a.close()
        backend_b.close()
        for line in connection_refusal_lines(exc, readonly=readonly):
            print(line, file=err)
        return False
    return True


def _reconcile_database(boot: ConfigBoot, *, machine: MachineIdentityReader) -> Database:
    """Capture this standalone entry's gated database at its original dial boundary."""
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process=process_name())
    return Database(
        db_config_from_boot(boot),
        gate=gate,
        direct_url=ClusterDirectUrl(machine.host),
    )


# meiva:note
# The two backends of the comparison, for the provider's vector space; writable only when asked.
def build_backends(
    args: ReconcileArguments, *, database: Database, provider: EmbeddingProvider, uri: str
) -> tuple[MemorySearchBackend, MemorySearchBackend]:
    backend_a, backend_b = (
        get_backend(
            name,
            database=database,
            dim=provider.dim,
            fingerprint=provider.fingerprint,
            uri=uri,
            readonly=not args.allow_write,
        )
        for name in (args.a, args.b)
    )
    return backend_a, backend_b


def parse_arguments(argv: Sequence[str]) -> ReconcileArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True, help="first backend name (e.g. numpy)")
    parser.add_argument("--b", required=True, help="second backend name (e.g. pgvector)")
    parser.add_argument("--limit", type=int, default=50, help="max sample queries")
    parser.add_argument("--k", type=int, default=10, help="top-k per query")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="allow writable backend connects after typing exact 'yes' interactively",
    )
    parsed = parser.parse_args(list(argv))
    return ReconcileArguments(
        a=parsed.a, b=parsed.b, limit=parsed.limit, k=parsed.k, allow_write=parsed.allow_write
    )


# meiva:note
# Run both backends over the same query texts and report where their orderings differ. The
# backends are connected here and always closed; the provider embeds each query once. Returns
# the process exit status: 0 compared (identical or not), 1 a backend could not be connected,
# 2 the two backends hold different row sets.
def run_comparison(
    args: ReconcileArguments,
    *,
    provider: EmbeddingProvider,
    backend_a: MemorySearchBackend,
    backend_b: MemorySearchBackend,
    queries: Sequence[str],
    out: TextIO,
    err: TextIO,
) -> int:
    if not _connect_backends(backend_a, backend_b, readonly=not args.allow_write, err=err):
        return 1
    try:
        gap = row_gap_lines(args.a, args.b, backend_a.all_meta(), backend_b.all_meta())
        if gap:
            for line in gap:
                print(line, file=err)
            return 2
        exact = 0
        for i, text in enumerate(queries, start=1):
            try:
                vector = provider.embed_query(text)
            except EmbeddingAPIError as exc:
                print(f"[{i}] embed failed, skipping: {exc}", file=err)
                continue
            paths_a = [hit.path for hit in backend_a.search_topk(vector, args.k)]
            paths_b = [hit.path for hit in backend_b.search_topk(vector, args.k)]
            lines = ordering_lines(i, text, args.a, args.b, paths_a, paths_b)
            if not lines:
                exact += 1
                continue
            for line in lines:
                print(line, file=out)
        print(summary_line(exact, len(queries), args.a, args.b, args.k), file=out)
    finally:
        backend_a.close()
        backend_b.close()
    return 0


def main(argv: Sequence[str], *, out: TextIO, err: TextIO) -> int:
    args = parse_arguments(argv)
    if args.allow_write:
        refusal = _confirmation_refusal()
        if refusal is not None:
            print(refusal, file=err)
            return 1

    boot = ConfigBoot()
    boot.read_process_environment()
    machine = ConfiguredMachineIdentity(boot)
    queries = sample_queries(args.limit, roles=machine.role(), home=meiva_home())
    if not queries:
        print("no query texts found in the memory pool", file=err)
        return 1
    print(f"{len(queries)} sample queries, k={args.k}", file=out)

    boot.boot()
    inputs = MemoryIndexerInputs.from_boot(boot)
    provider = get_provider(
        inputs.embedding_name,
        catalog=build_model_catalog(),
        timeout_s=inputs.embed_timeout_s,
        api_key=inputs.api_key,
    )
    database = _reconcile_database(boot, machine=machine)
    backend_a, backend_b = build_backends(
        args, database=database, provider=provider, uri=inputs.search_uri
    )
    return run_comparison(
        args,
        provider=provider,
        backend_a=backend_a,
        backend_b=backend_b,
        queries=queries,
        out=out,
        err=err,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], out=sys.stdout, err=sys.stderr))
