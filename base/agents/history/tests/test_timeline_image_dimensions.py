from __future__ import annotations

import base64
from io import BytesIO

from langchain_core.messages import HumanMessage
from PIL import Image

from base.agents.history.timeline import build_timeline_items
from base.agents.messages.image_dimensions import ImageDimensions
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import Clock


def _png_data_uri(width: int, height: int) -> str:
    buf = BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="PNG")
    encoded = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _policy() -> TimestampPolicy:
    return TimestampPolicy.from_clock(Clock.from_settings(), timestamps_enabled=False)


def test_inbound_item_reads_meiva_image_dimensions_and_preserves_none_holes() -> None:
    msg = HumanMessage(
        content="two uploads",
        additional_kwargs={
            "meiva_msg_type": MeivaMsgType.INBOUND.value,
            "meiva_source": "user",
            "meiva_inbound_body_start": 0,
            "meiva_image_urls": [
                "/api/agents/7/uploads/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/0",
                "/api/agents/7/uploads/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/1",
            ],
            "meiva_image_dimensions": [{"width": 1024, "height": 768}, None],
        },
    )

    items, _ = build_timeline_items([msg], inputs=_policy())

    assert len(items) == 1
    assert items[0].image_dimensions == [ImageDimensions(width=1024, height=768), None]


def test_legacy_inbound_item_falls_back_to_inlined_data_uri_dimensions() -> None:
    uri1 = _png_data_uri(320, 240)
    uri2 = _png_data_uri(600, 900)
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "legacy multimodal"},
            {"type": "image_url", "image_url": {"url": uri1}},
            {"type": "image_url", "image_url": {"url": uri2}},
        ],
        additional_kwargs={
            "meiva_msg_type": MeivaMsgType.INBOUND.value,
            "meiva_source": "user",
            "meiva_inbound_body_start": 0,
            "meiva_image_urls": [
                "/api/agents/7/uploads/legacy-a.png",
                "/api/agents/7/uploads/legacy-b.png",
            ],
        },
    )

    items, _ = build_timeline_items([msg], inputs=_policy())

    assert len(items) == 1
    assert items[0].image_dimensions == [
        ImageDimensions(width=320, height=240),
        ImageDimensions(width=600, height=900),
    ]


def test_attach_item_extracts_dimensions_for_each_data_uri() -> None:
    uri_ok = _png_data_uri(500, 250)
    uri_bad = "data:image/png;base64,bm90LWEtcG5n"
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "Files attached during this turn:"},
            {"type": "text", "text": "- [1] chart.png"},
            {"type": "image_url", "image_url": {"url": uri_ok}},
            {"type": "text", "text": "- [2] bad.png"},
            {"type": "image_url", "image_url": {"url": uri_bad}},
        ],
        additional_kwargs={"meiva_msg_type": MeivaMsgType.ATTACH.value},
    )

    items, _ = build_timeline_items([msg], inputs=_policy())

    assert len(items) == 1
    assert items[0].images == [uri_ok, uri_bad]
    assert items[0].image_dimensions == [ImageDimensions(width=500, height=250), None]
