import { describe, expect, it } from "vitest";

import { formatRelative, formatUpcoming, normalizeDayPeriodSpace } from "./time";

const SHANGHAI = "Asia/Shanghai";
const NEW_YORK = "America/New_York";

const NOW = new Date("2026-10-17T16:26:00+08:00");

function upcoming(iso: string, now: Date = NOW, timeZone = SHANGHAI): string {
  return formatUpcoming(iso, now, { timeZone });
}

describe("formatUpcoming day context", () => {
  it("keeps a later-today instant as a bare clock time", () => {
    expect(upcoming("2026-10-17T17:30:00+08:00")).toBe("5:30 PM");
  });

  it("labels tomorrow instead of showing a clock time alone (the reported case)", () => {
    expect(upcoming("2026-10-18T15:21:00+08:00")).toBe("Tomorrow 3:21 PM");
  });

  it("calls 00:01 tomorrow when the local date changes, 20 minutes from now", () => {
    const now = new Date("2026-10-17T23:59:00+08:00");
    expect(upcoming("2026-10-18T00:01:00+08:00", now)).toBe("Tomorrow 12:01 AM");
  });

  it("keeps today's clock for a target 23h59m away that stays on the same date", () => {
    const now = new Date("2026-10-18T00:00:00+08:00");
    expect(upcoming("2026-10-18T23:59:00+08:00", now)).toBe("11:59 PM");
  });

  it("names the weekday two days out", () => {
    const now = new Date("2026-10-18T16:00:00+08:00");
    expect(upcoming("2026-10-20T15:21:00+08:00", now)).toBe("Tue 3:21 PM");
  });

  it("still names the weekday at the tier's last day", () => {
    expect(upcoming("2026-10-23T09:05:00+08:00")).toBe("Fri 9:05 AM");
  });

  it("switches to a dated countdown at exactly seven days", () => {
    expect(upcoming("2026-10-24T15:21:00+08:00")).toBe("In 7 days (Oct 24)");
  });

  it("keeps the countdown on the last day before the date tier", () => {
    expect(upcoming("2026-11-15T09:05:00+08:00")).toBe("In 29 days (Nov 15)");
  });

  it("renders a date at exactly one month (30 days)", () => {
    expect(upcoming("2026-11-16T15:21:00+08:00")).toBe("Nov 16");
  });

  it("appends the year when the target falls in another calendar year", () => {
    const now = new Date("2026-12-01T12:00:00+08:00");
    expect(upcoming("2027-01-05T09:00:00+08:00", now)).toBe("Jan 5, 2027");
  });

  it("counts a spring-forward day as one calendar day, not 24 hours", () => {
    const now = new Date("2026-03-07T20:00:00-05:00");
    expect(upcoming("2026-03-08T20:30:00-04:00", now, NEW_YORK)).toBe("Tomorrow 8:30 PM");
  });

  it("counts a fall-back day as one calendar day, not 25 hours", () => {
    const now = new Date("2026-11-01T00:30:00-04:00");
    expect(upcoming("2026-11-02T00:10:00-05:00", now, NEW_YORK)).toBe("Tomorrow 12:10 AM");
  });

  it("keeps today's clock across a 24.5-hour fall-back day", () => {
    const now = new Date("2026-11-01T00:30:00-04:00");
    expect(upcoming("2026-11-01T23:30:00-05:00", now, NEW_YORK)).toBe("11:30 PM");
  });
});

describe("formatUpcoming input handling", () => {
  it("keeps the existing relative wording for a past instant", () => {
    expect(upcoming("2026-10-17T16:21:00+08:00")).toBe("5m ago");
  });

  it("returns an em dash for an unparseable instant", () => {
    expect(upcoming("not-a-timestamp")).toBe("—");
  });

  it("defaults to the viewer's zone (the suite pins TZ=UTC)", () => {
    expect(formatUpcoming("2026-10-18T15:21:00Z", new Date("2026-10-17T16:26:00Z"))).toBe(
      "Tomorrow 3:21 PM",
    );
  });

  it("judges the day boundary in the requested zone, not the process zone", () => {
    const now = new Date("2026-10-17T16:30:00Z");
    const target = "2026-10-18T00:15:00Z";
    expect(formatUpcoming(target, now, { timeZone: "UTC" })).toBe("Tomorrow 12:15 AM");
    expect(formatUpcoming(target, now, { timeZone: SHANGHAI })).toBe("8:15 AM");
  });

});

describe("normalizeDayPeriodSpace", () => {
  it("turns ICU's narrow no-break space into a plain space", () => {
    expect(normalizeDayPeriodSpace("3:21\u202fPM")).toBe("3:21 PM");
  });

  it("turns a no-break space into a plain space", () => {
    expect(normalizeDayPeriodSpace("3:21\u00a0PM")).toBe("3:21 PM");
  });

  it("leaves an already-plain clock unchanged", () => {
    expect(normalizeDayPeriodSpace("3:21 PM")).toBe("3:21 PM");
  });
});

describe("formatRelative sub-minute bucket", () => {
  it("says 'just now' at zero seconds instead of a bare 'now'", () => {
    expect(formatRelative(NOW.toISOString(), NOW)).toBe("just now");
  });

  it("still says 'just now' at 59 seconds", () => {
    expect(formatRelative(new Date(NOW.getTime() - 59_000).toISOString(), NOW)).toBe("just now");
  });

  it("leaves the 60-second boundary on the minute tier", () => {
    expect(formatRelative(new Date(NOW.getTime() - 60_000).toISOString(), NOW)).toBe("1m ago");
  });

  it("says 'just now' for a future instant still under a minute", () => {
    expect(formatRelative(new Date(NOW.getTime() + 30_000).toISOString(), NOW)).toBe("just now");
  });
});
