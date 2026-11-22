from __future__ import annotations

from PIL import Image, ImageDraw

from .constants import (
    OVERLAY_BORDER_PX,
    OVERLAY_COLOR_RGB,
    OVERLAY_DASH_PX,
    OVERLAY_GAP_PX,
)


def build_dashed_border_bgra(
    width: int,
    height: int,
    *,
    color_rgb: tuple[int, int, int] = OVERLAY_COLOR_RGB,
    border_px: int = OVERLAY_BORDER_PX,
    dash_px: int = OVERLAY_DASH_PX,
    gap_px: int = OVERLAY_GAP_PX,
) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("overlay dimensions must be positive")
    r, g, b = color_rgb
    t = max(2, int(border_px))
    dash = max(4, int(dash_px))
    gap = max(2, int(gap_px))
    step = dash + gap

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bgra = (b, g, r, 255)

    for x in range(0, width, step):
        x2 = min(width - 1, x + dash - 1)
        draw.rectangle([x, 0, x2, t - 1], fill=bgra)
        draw.rectangle([x, height - t, x2, height - 1], fill=bgra)

    for y in range(0, height, step):
        y2 = min(height - 1, y + dash - 1)
        draw.rectangle([0, y, t - 1, y2], fill=bgra)
        draw.rectangle([width - t, y, width - 1, y2], fill=bgra)

    return img.tobytes()
