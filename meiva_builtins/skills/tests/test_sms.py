# meiva:note
# Hermetic unit tests for the sms skill (meiva_builtins/skills/integrations/sms/scripts).
#
# The skill's live behavior (reading the real ~/Library/Messages/chat.db under macOS Full Disk
# Access) can't run in CI; that path is verified by hand. The decisions are tested as values
# (the code patterns, the query plan, the row mapping, the printed lines) and the shell runs
# against a real SQLite file under tmp_path with the same message/handle schema, so nothing
# touches the user's real data.


from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

messages = load_skill_script(
    "integrations", "sms", "scripts", "sms_messages.py", name="sms_messages"
)
sms = load_skill_script("integrations", "sms", "scripts", "query.py", name="sms_query")

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
WECHAT_CODE = "\u3010\u5fae\u4fe1\u3011\u9a8c\u8bc1\u7801 654321\uff0c\u8bf7\u52ff\u6cc4\u9732"


def _apple_ns(dt: datetime) -> int:
    return int((dt.timestamp() - messages.APPLE_EPOCH_OFFSET) * 1_000_000_000)


def _write_chat_db(db: Path, now: datetime) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER,
          is_from_me INTEGER, service TEXT, handle_id INTEGER);
        """
    )
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15551231118')")
    rows = [
        (WECHAT_CODE, now - timedelta(minutes=5), 0, "SMS", 1),
        ("G-778899 is your Google verification code", now - timedelta(minutes=10), 0, "SMS", 1),
        ("hey are we still on for lunch?", now - timedelta(minutes=15), 0, "SMS", 1),
        ("old code 111111", now - timedelta(hours=48), 0, "SMS", 1),
    ]
    for text, dt, is_from_me, service, handle_id in rows:
        conn.execute(
            "INSERT INTO message (text, date, is_from_me, service, handle_id) VALUES (?, ?, ?, ?, ?)",
            (text, _apple_ns(dt), is_from_me, service, handle_id),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def chat_db(tmp_path: Path) -> Path:
    db = tmp_path / "chat.db"
    _write_chat_db(db, NOW)
    return db


def _filt(
    *,
    codes_only: bool = False,
    phone_suffix: str | None = None,
    lookback_hours: float = messages.DEFAULT_LOOKBACK_HOURS,
    limit: int = messages.DEFAULT_LIMIT,
) -> object:
    return messages.ChatFilter(
        phone_suffix=phone_suffix,
        lookback_hours=lookback_hours,
        limit=limit,
        codes_only=codes_only,
    )


def test_extract_codes_multilingual() -> None:
    assert messages.extract_codes(WECHAT_CODE) == ["654321"]
    assert messages.extract_codes("G-778899 is your Google verification code") == ["778899"]
    assert messages.extract_codes("hey are we still on for lunch?") == []
    assert messages.extract_codes("") == []


def test_extract_codes_keeps_first_seen_order_without_repeats() -> None:
    assert messages.extract_codes("code: 1234 then [5678] and 1234 is your code") == [
        "1234",
        "5678",
    ]


def test_query_plan_windows_the_lookback_and_binds_the_page() -> None:
    plan = messages.build_chat_query(_filt(limit=7), NOW)
    cutoff = _apple_ns(NOW - timedelta(hours=12))
    assert plan.params == (cutoff, 7)
    assert plan.sql.rstrip().endswith("ORDER BY m.date DESC LIMIT ?")
    assert "LIKE" not in plan.sql


def test_codes_only_query_scans_a_wider_candidate_window() -> None:
    assert messages.build_chat_query(_filt(codes_only=True, limit=3), NOW).params[-1] == 50
    assert messages.build_chat_query(_filt(codes_only=True, limit=20), NOW).params[-1] == 100
    assert messages.build_chat_query(_filt(codes_only=False, limit=20), NOW).params[-1] == 20


def test_phone_suffix_binds_a_literal_suffix_pattern() -> None:
    plan = messages.build_chat_query(_filt(phone_suffix="1118", limit=5), NOW)
    assert "h.id LIKE ?" in plan.sql
    assert plan.params == (_apple_ns(NOW - timedelta(hours=12)), "%1118", 5)


def test_phone_suffix_wildcards_are_escaped() -> None:
    plan = messages.build_chat_query(_filt(phone_suffix="5_5%"), NOW)
    assert plan.params[1] == "%5\\_5\\%"


@pytest.mark.parametrize("limit", [0, -1])
def test_a_page_that_selects_nothing_is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        _filt(limit=limit)


@pytest.mark.parametrize("hours", [0, -3])
def test_a_window_that_selects_nothing_is_refused(hours: float) -> None:
    with pytest.raises(ValueError, match="lookback"):
        _filt(lookback_hours=hours)


def _row(text: str, date: int, *, id: int = 1, is_from_me: int = 0) -> object:
    return messages.ChatRow(
        id=id, text=text, date=date, is_from_me=is_from_me, service="SMS", phone="+15551231118"
    )


def test_select_messages_maps_rows_and_dates() -> None:
    ns = _apple_ns(NOW)
    [from_ns, from_seconds] = messages.select_messages(
        [_row(WECHAT_CODE, ns), _row("plain", ns // 1_000_000_000, id=2, is_from_me=1)], _filt()
    )
    assert from_ns.date == NOW.isoformat()
    assert from_seconds.date == NOW.isoformat()
    assert from_ns.codes == ("654321",) and from_seconds.codes == ()
    assert from_seconds.is_from_me is True
    assert (from_ns.id, from_ns.phone, from_ns.service) == (1, "+15551231118", "SMS")


def test_select_messages_codes_only_drops_rows_without_a_code() -> None:
    ns = _apple_ns(NOW)
    rows = [_row("lunch?", ns), _row(WECHAT_CODE, ns, id=2)]
    assert [m.id for m in messages.select_messages(rows, _filt(codes_only=True))] == [2]
    assert [m.id for m in messages.select_messages(rows, _filt(codes_only=False))] == [1, 2]


def test_select_messages_stops_at_the_limit() -> None:
    ns = _apple_ns(NOW)
    rows = [_row(WECHAT_CODE, ns, id=i) for i in range(1, 6)]
    assert [m.id for m in messages.select_messages(rows, _filt(limit=2))] == [1, 2]


def test_printed_lines() -> None:
    [code_msg] = messages.select_messages([_row(WECHAT_CODE, _apple_ns(NOW))], _filt())
    assert messages.format_code_line(code_msg) == (
        f"[{NOW.isoformat()}] codes=654321 | +15551231118: {WECHAT_CODE}"
    )
    assert messages.format_message_line(code_msg) == (
        f"[{NOW.isoformat()}] From +15551231118: {WECHAT_CODE}"
    )


def test_query_codes_returns_only_code_bearing_rows(chat_db: Path) -> None:
    codes = sms.query_messages(chat_db, NOW, _filt(codes_only=True))
    assert [c.codes for c in codes] == [
        ("654321",),
        ("778899",),
    ]
    assert all(c.service == "SMS" and c.phone == "+15551231118" for c in codes)


def test_query_messages_respects_lookback(chat_db: Path) -> None:
    # meiva:note
    # 3 within the 12h default; the 48h-old row is excluded.
    assert len(sms.query_messages(chat_db, NOW, _filt())) == 3
    # meiva:note
    # Widen the window and the old row appears.
    assert len(sms.query_messages(chat_db, NOW, _filt(lookback_hours=72))) == 4


def test_phone_suffix_filter(chat_db: Path) -> None:
    assert len(sms.query_messages(chat_db, NOW, _filt(phone_suffix="1118"))) == 3
    assert sms.query_messages(chat_db, NOW, _filt(phone_suffix="9999")) == []


def test_phone_suffix_wildcards_do_not_match_everything(chat_db: Path) -> None:
    assert sms.query_messages(chat_db, NOW, _filt(phone_suffix="%")) == []
    assert sms.query_messages(chat_db, NOW, _filt(phone_suffix="_118")) == []


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sms.query_messages(tmp_path / "nope.db", NOW, _filt(codes_only=True))


def test_main_prints_one_line_per_code(chat_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sms.main(["--recent-codes"], chat_db=chat_db, now=NOW)
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("|")[0].strip() for line in lines] == [
        f"[{(NOW - timedelta(minutes=5)).isoformat()}] codes=654321",
        f"[{(NOW - timedelta(minutes=10)).isoformat()}] codes=778899",
    ]


def test_main_reports_no_codes(chat_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sms.main(["--recent-codes", "--phone-suffix", "9999"], chat_db=chat_db, now=NOW)
    assert capsys.readouterr().out == "No verification codes found.\n"


def test_main_lists_recent_messages(chat_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sms.main(["--recent-messages", "--limit", "2"], chat_db=chat_db, now=NOW)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(" From +15551231118: " in line for line in lines)


def test_main_reports_no_messages(chat_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sms.main(["--recent-messages", "--lookback-hours", "0.001"], chat_db=chat_db, now=NOW)
    assert capsys.readouterr().out == "No messages found.\n"


def test_main_without_a_lens_prints_help_to_stderr(
    chat_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sms.main([], chat_db=chat_db, now=NOW)
    captured = capsys.readouterr()
    assert captured.out == "" and "--recent-codes" in captured.err


def test_main_refuses_an_empty_page_as_a_usage_error(
    chat_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        sms.main(["--recent-messages", "--limit", "0"], chat_db=chat_db, now=NOW)
    assert exit_info.value.code == 2
    assert "limit must be at least 1" in capsys.readouterr().err


def test_script_reads_the_chat_db_under_the_home_it_runs_with(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "Library" / "Messages").mkdir(parents=True)
    _write_chat_db(home / "Library" / "Messages" / "chat.db", datetime.now(tz=UTC))
    # meiva:note
    # The argv is this interpreter and the skill script inside the repository, never input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(sms.__file__), "--recent-codes", "--limit", "1"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("codes=654321") == 1
