#!/usr/bin/env python3
"""Read SMS/iMessage 2FA codes and recent messages from macOS Messages.app.

Self-contained skill script — run from the repo source root with the venv Python:

    .venv/bin/python skills/sms/scripts/query.py --recent-codes
    .venv/bin/python skills/sms/scripts/query.py --recent-codes --phone-suffix 1118
    .venv/bin/python skills/sms/scripts/query.py --recent-messages --limit 10
    .venv/bin/python skills/sms/scripts/query.py --recent-codes --lookback-hours 24

macOS only. Reads ~/Library/Messages/chat.db (SQLite) directly, so the calling
process needs Full Disk Access. When TCC blocks that read the sqlite3
OperationalError propagates to the caller — there is no fallback (fail fast).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from base.host.boundary.options import options
from base.host.boundary.untyped import object_of

# meiva:note
# `sms_messages` is a sibling in this skill's own scripts/ dir, not an importable package
# (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path). Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sms_messages import (
    DEFAULT_LIMIT,
    DEFAULT_LOOKBACK_HOURS,
    ChatFilter,
    ChatQuery,
    ChatRow,
    SmsMessage,
    build_chat_query,
    format_code_line,
    format_message_line,
    parse_rows,
    select_messages,
)


# meiva:note
# Run `query` against the chat.db at `db_path` and return its rows. A missing database
# raises FileNotFoundError; a TCC-blocked read raises sqlite3.OperationalError to the caller.
def fetch_rows(db_path: Path, query: ChatQuery) -> list[ChatRow]:
    if not db_path.exists():
        raise FileNotFoundError(f"Messages database not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        # meiva:note
        # `fetchall` answers `Any`; `object_of` hands the rows over as one `object` for
        # `parse_rows` to check column by column.
        return parse_rows(object_of(conn.execute(query.sql, query.params).fetchall))
    finally:
        conn.close()


# meiva:note
# The messages `filt` selects from the chat.db at `db_path`, as of `now`, newest first.
def query_messages(db_path: Path, now: datetime, filt: ChatFilter) -> list[SmsMessage]:
    return select_messages(fetch_rows(db_path, build_chat_query(filt, now)), filt)


# meiva:note
# The five flags `main` reads, one field per namespace attribute; `chat_db` and `now` are not
# fields because the caller binds them rather than the parser.
@dataclass(frozen=True)
class _QueryOptions:
    recent_codes: bool
    recent_messages: bool
    phone_suffix: str | None
    lookback_hours: float
    limit: int


# meiva:note
# The CLI: parse `argv`, query the chat.db at `chat_db` as of `now`, print one line per
# message. A limit or lookback that selects nothing is refused as a usage error.
def main(argv: Sequence[str], *, chat_db: Path, now: datetime) -> None:
    parser = argparse.ArgumentParser(description="Query SMS from Messages.app")
    parser.add_argument("--recent-codes", action="store_true", help="Get recent verification codes")
    parser.add_argument("--recent-messages", action="store_true", help="Get recent SMS messages")
    parser.add_argument("--phone-suffix", type=str, default=None, help="Filter by last N digits")
    # meiva:note
    # argparse converts only a *string* default through `type`; this flag declares a float, so its
    # default is written as one.
    parser.add_argument("--lookback-hours", type=float, default=float(DEFAULT_LOOKBACK_HOURS))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max messages")
    opts = options(parser.parse_args(argv), _QueryOptions)

    if not (opts.recent_codes or opts.recent_messages):
        parser.print_help(sys.stderr)
        return
    try:
        filt = ChatFilter(
            phone_suffix=opts.phone_suffix,
            lookback_hours=opts.lookback_hours,
            limit=opts.limit,
            codes_only=opts.recent_codes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    results = query_messages(chat_db, now, filt)
    if opts.recent_codes:
        for message in results:
            print(format_code_line(message))
        if not results:
            print("No verification codes found.")
    else:
        for message in results:
            print(format_message_line(message))
        if not results:
            print("No messages found.")


if __name__ == "__main__":
    main(
        sys.argv[1:],
        chat_db=Path.home() / "Library" / "Messages" / "chat.db",
        now=datetime.now(tz=UTC),
    )
