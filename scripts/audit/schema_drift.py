#!/usr/bin/env python3
# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from meizel import lint_common
from meizel.checkout import apply_plan
from meizel.graph.universe import EditPlan
from scripts.audit.analysis import allowlist_entries, compare, live_columns, schema_columns


def _live_rows(conn: psycopg.Connection) -> list[tuple[str, str, str, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
        return list(cur.fetchall())


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


def _allow_text(allow: Path | None) -> str:
    # meiva:note
    # The allowlist as read from disk; an absent or unreadable file allows nothing.
    if allow is None:
        return ""
    allow_text = lint_common.read_utf8_text(allow)
    return allow_text or ""


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
        live = live_columns(_live_rows(conn))
    declared = {name.lower() for name in schema}
    drift = compare(schema, live).allowed_only(allowlist_entries(_allow_text(args.allow)))
    print(drift.render(declared))
    if args.json is not None:
        apply_plan(EditPlan(writes={args.json: json.dumps(drift.to_json(declared), indent=2)}))
    return int(drift.is_dirty())


if __name__ == "__main__":
    sys.exit(main())
