import { describe, expect, it } from "vitest";

import {
  runTimelineContextPath,
  runTimelineLinkContentPath,
  runTimelineLinksPath,
  runTimelineMessagesPath,
  runTimelinePath,
  timelinePath,
} from "./timeline-routes";

describe("timeline routes", () => {
  it("getTimeline has no query by default", () => {
    expect(timelinePath(22)).toBe("/api/agents/22/timeline");
  });

  it("getTimeline forwards limit + before query params", () => {
    expect(timelinePath(22, { limit: 100, before: "12.0" })).toBe("/api/agents/22/timeline?limit=100&before=12.0");
  });

  it("run timeline carries the selected window", () => {
    expect(runTimelinePath(405, {
      from: "2026-08-29T08:00:00.000Z",
      to: "2026-08-29T09:00:00.000Z",
    })).toBe("/api/agents/405/run-timeline?from=2026-08-29T08%3A00%3A00.000Z&to=2026-08-29T09%3A00%3A00.000Z");
  });

  it("run timeline sends no query without a window (the whole lifetime)", () => {
    expect(runTimelinePath(405)).toBe("/api/agents/405/run-timeline");
  });

  it("requests bounded recent history and pins later range/detail reads to its snapshot", () => {
    expect(runTimelinePath(405, { recentHours: 24, maxUnits: 5000 })).toMatch(/recent_hours=24&max_units=5000$/);
    expect(runTimelinePath(405, {
      from: "2026-10-01T00:00:00Z",
      to: "2026-10-02T00:00:00Z",
      maxUnits: 5000,
      snapshot: "v1",
    })).toMatch(/max_units=5000&snapshot=v1$/);
    expect(runTimelineContextPath(405, 10, "v1")).toBe("/api/agents/405/run-timeline/context?at=10&snapshot=v1");
    expect(runTimelineContextPath(405, 10)).toBe("/api/agents/405/run-timeline/context?at=10");
    expect(runTimelineMessagesPath(405, { start: 10, end: 11, snapshot: "v1" }))
      .toBe("/api/agents/405/run-timeline/messages?start=10&end=11&snapshot=v1");
  });

  it("links address the events of a set of agents in a window", () => {
    expect(runTimelineLinksPath([405, 6657], { from: "2026-10-03T00:00:00.000Z", to: "2026-10-05T00:00:00.000Z" }))
      .toBe("/api/insights/run-timeline/links?agents=405%2C6657&from=2026-10-03T00%3A00%3A00.000Z&to=2026-10-05T00%3A00%3A00.000Z");
  });

  it("link content addresses one message or notice by its reference", () => {
    expect(runTimelineLinkContentPath({ inbound_id: 31 })).toBe("/api/insights/run-timeline/link-content?inbound_id=31");
    expect(runTimelineLinkContentPath({ notice_id: 4 })).toBe("/api/insights/run-timeline/link-content?notice_id=4");
  });

  it("messages are an index range, uncut only when asked", () => {
    expect(runTimelineMessagesPath(405, { start: 8, end: 68, limit: 50 }))
      .toBe("/api/agents/405/run-timeline/messages?start=8&end=68&limit=50");
    expect(runTimelineMessagesPath(405, { start: 8, end: 68, full: true }))
      .toBe("/api/agents/405/run-timeline/messages?start=8&end=68&full=true");
  });
});
