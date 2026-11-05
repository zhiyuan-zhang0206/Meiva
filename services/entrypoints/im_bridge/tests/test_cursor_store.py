from __future__ import annotations

from psycopg_pool import ConnectionPool

from services.entrypoints.im_bridge.cursor_store import CursorStore, PushWatermark


def test_load_push_carries_created_at_and_none_for_legacy(pool: ConnectionPool) -> None:
    store = CursorStore(pool)
    store.save_push("telegram", "12345", 405, PushWatermark("2026-10-03T16:21:00+00:00", "128.1"))
    store.save_push("weixin", "peer_1", 1818, PushWatermark(None, "377.1"))

    assert store.load_push() == {
        ("telegram", "12345", 405): PushWatermark("2026-10-03T16:21:00+00:00", "128.1"),
        ("weixin", "peer_1", 1818): PushWatermark(None, "377.1"),
    }


def test_save_push_overwrites_both_keys(pool: ConnectionPool) -> None:
    store = CursorStore(pool)
    store.save_push("telegram", "12345", 405, PushWatermark("2026-10-03T16:21:00+00:00", "128.1"))
    store.save_push("telegram", "12345", 405, PushWatermark("2026-10-03T16:30:00+00:00", "129.0"))

    assert store.load_push() == {
        ("telegram", "12345", 405): PushWatermark("2026-10-03T16:30:00+00:00", "129.0")
    }
