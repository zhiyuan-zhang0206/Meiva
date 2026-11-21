from __future__ import annotations

from datetime import UTC, datetime

from base.agents.history.transcript_timeline.sent import SentRow, row_item
from base.agents.messages.image_dimensions import ImageDimensions


def test_row_item_aligns_image_dimensions_and_upload_file_dimensions() -> None:
    batch_id = "c" * 32
    row = SentRow(
        id=42,
        agent_id=9,
        kind="chat",
        source="user",
        content="check these",
        payload={
            "image_uploads": [
                {
                    "name": "a.png",
                    "size": 1200,
                    "content_type": "image/png",
                    "batch_id": batch_id,
                    "ordinal": 0,
                    "width": 800,
                    "height": 600,
                },
                {
                    "name": "b.png",
                    "size": 900,
                    "content_type": "image/png",
                    "batch_id": batch_id,
                    "ordinal": 1,
                },
            ],
            "upload_files": [
                {
                    "name": "preview.bmp",
                    "size": 2048,
                    "content_type": "image/bmp",
                    "batch_id": batch_id,
                    "ordinal": 2,
                    "width": 320,
                    "height": 180,
                }
            ],
        },
        created_at=datetime(2026, 11, 21, 13, 0, 0, tzinfo=UTC),
    )

    item = row_item(row)

    assert item is not None
    assert item.images == [
        f"/api/agents/9/uploads/{batch_id}/0",
        f"/api/agents/9/uploads/{batch_id}/1",
    ]
    assert item.image_dimensions == [ImageDimensions(width=800, height=600), None]
    assert item.files is not None and len(item.files) == 1
    assert (item.files[0].width, item.files[0].height) == (320, 180)


def test_row_item_returns_none_image_dimensions_when_legacy_uploads_lack_sizes() -> None:
    batch_id = "d" * 32
    row = SentRow(
        id=43,
        agent_id=9,
        kind="chat",
        source="user",
        content="legacy",
        payload={
            "image_uploads": [
                {
                    "name": "old.png",
                    "size": 500,
                    "content_type": "image/png",
                    "batch_id": batch_id,
                    "ordinal": 0,
                }
            ]
        },
        created_at=datetime(2026, 11, 21, 13, 0, 0, tzinfo=UTC),
    )

    item = row_item(row)

    assert item is not None
    assert item.images == [f"/api/agents/9/uploads/{batch_id}/0"]
    assert item.image_dimensions is None
