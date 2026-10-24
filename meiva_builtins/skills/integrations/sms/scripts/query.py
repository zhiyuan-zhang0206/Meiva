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
from datetime import UTC, datetime
from pathlib import Path

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
        return [ChatRow(*row) for row in conn.execute(query.sql, query.params).fetchall()]
    finally:
        conn.close()


# meiva:note
# The messages `filt` selects from the chat.db at `db_path`, as of `now`, newest first.
def query_messages(db_path: Path, now: datetime, filt: ChatFilter) -> list[SmsMessage]:
    return select_messages(fetch_rows(db_path, build_chat_query(filt, now)), filt)


# meiva:note
# The CLI: parse `argv`, query the chat.db at `chat_db` as of `now`, print one line per
# message. A limit or lookback that selects nothing is refused as a usage error.
def main(argv: Sequence[str], *, chat_db: Path, now: datetime) -> None:
    parser = argparse.ArgumentParser(description="Query SMS from Messages.app")
    parser.add_argument("--recent-codes", action="store_true", help="Get recent verification codes")
    parser.add_argument("--recent-messages", action="store_true", help="Get recent SMS messages")
    parser.add_argument("--phone-suffix", type=str, default=None, help="Filter by last N digits")
    parser.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max messages")
    args = parser.parse_args(argv)

    if not (args.recent_codes or args.recent_messages):
        parser.print_help(sys.stderr)
        return
    try:
        filt = ChatFilter(
            phone_suffix=args.phone_suffix,
            lookback_hours=args.lookback_hours,
            limit=args.limit,
            codes_only=args.recent_codes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    results = query_messages(chat_db, now, filt)
    if args.recent_codes:
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
