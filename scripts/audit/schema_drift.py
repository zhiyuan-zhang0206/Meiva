#!/usr/bin/env python3
# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from meizel import lint_common
from meizel.graph.universe import EditPlan
from scripts.audit.analysis import compare, schema_columns


def _live_columns(conn: psycopg.Connection) -> dict[str, dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
        out: dict[str, dict[str, str]] = {}
        for table, column, nullable, default in cur.fetchall():
            out.setdefault(table, {})[column] = f"{nullable}|{default or ''}"
        return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare live columns against db/schema.sql.")
    parser.add_argument("--schema", type=Path, default=Path("db/schema.sql"))
    parser.add_argument(
        "--dsn", default=None, help="Postgres URL; defaults to the deployment settings"
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--allow",
        type=Path,
        default=None,
        help="file listing table names and table.column entries to ignore",
    )
    return parser.parse_args(argv)


def _allowed_entries(allow: Path | None) -> frozenset[str]:
    # meiva:note
    # The entries the allowlist names; an absent file allows nothing.
    if allow is None:
        return frozenset()
    allow_text = lint_common.read_utf8_text(allow)
    if allow_text is None:
        return frozenset()
    return frozenset(line.strip() for line in allow_text.splitlines() if line.strip())


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.dsn is not None:
        dsn = str(args.dsn)
    else:
        from base.config import settings

        dsn = settings.data_plane.db_url
    schema_text = lint_common.read_utf8_text(args.schema)
    if schema_text is None:
        raise FileNotFoundError(args.schema)
    schema = schema_columns(schema_text)
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        live = _live_columns(conn)
    declared = {name.lower() for name in schema}
    drift = compare(schema, live)
    allowed = _allowed_entries(args.allow)
    if allowed:
        drift = drift.allowed_only(allowed)
    print(drift.render(declared))
    if args.json is not None:
        EditPlan(writes={args.json: json.dumps(drift.to_json(declared), indent=2)}).apply()
    return int(drift.is_dirty())


if __name__ == "__main__":
    sys.exit(main())
