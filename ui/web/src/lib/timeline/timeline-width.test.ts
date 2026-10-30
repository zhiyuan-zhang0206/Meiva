import { describe, expect, it } from "vitest";

import {
  clampTimelineWidthPx,
  TIMELINE_NARROW_BREAKPOINT_PX,
  TIMELINE_WIDTH_PX_DEFAULT,
  TIMELINE_WIDTH_PX_MAX,
  TIMELINE_WIDTH_PX_MIN,
  timelineMaxWidthCss,
} from "./timeline-width";

describe("TIMELINE_NARROW_BREAKPOINT_PX", () => {
  it("is the md breakpoint (768px)", () => {
    expect(TIMELINE_NARROW_BREAKPOINT_PX).toBe(768);
  });
});

describe("clampTimelineWidthPx", () => {
  it("keeps in-range pixel values", () => {
    expect(clampTimelineWidthPx(768)).toBe(768);
    expect(clampTimelineWidthPx(TIMELINE_WIDTH_PX_MIN)).toBe(TIMELINE_WIDTH_PX_MIN);
    expect(clampTimelineWidthPx(TIMELINE_WIDTH_PX_MAX)).toBe(TIMELINE_WIDTH_PX_MAX);
  });

  it("clamps out-of-range values to [MIN, MAX]", () => {
    expect(clampTimelineWidthPx(300)).toBe(TIMELINE_WIDTH_PX_MIN);
    expect(clampTimelineWidthPx(1600)).toBe(TIMELINE_WIDTH_PX_MAX);
  });

  it("falls back to the default for non-numbers / corrupt values", () => {
    expect(clampTimelineWidthPx(undefined)).toBe(TIMELINE_WIDTH_PX_DEFAULT);
    expect(clampTimelineWidthPx(null)).toBe(TIMELINE_WIDTH_PX_DEFAULT);
    expect(clampTimelineWidthPx("768")).toBe(TIMELINE_WIDTH_PX_DEFAULT);
    expect(clampTimelineWidthPx(Number.NaN)).toBe(TIMELINE_WIDTH_PX_DEFAULT);
  });
});

describe("timelineMaxWidthCss", () => {
  it("renders a pixel max-width for the default width", () => {
    expect(timelineMaxWidthCss(768)).toBe("768px");
  });

  it("renders the configured pixel ceiling", () => {
    expect(timelineMaxWidthCss(960)).toBe("960px");
    expect(timelineMaxWidthCss(1280)).toBe("1280px");
  });

  it("clamps values above the ceiling to TIMELINE_WIDTH_PX_MAX", () => {
    expect(timelineMaxWidthCss(1600)).toBe("1280px");
  });

  it("falls back to the default pixel width for a missing value", () => {
    expect(timelineMaxWidthCss(undefined)).toBe("768px");
  });
});
