import { describe, expect, it } from "vitest";

import {
  formatAbsolute,
  formatClockText,
  formatRelative,
  formatShort,
  formatUpcoming,
  normalizeDayPeriodSpace,
} from "./time";

const SHANGHAI = "Asia/Shanghai";
const NEW_YORK = "America/New_York";

const NOW = new Date("2026-10-17T16:26:00+08:00");

function upcoming(
  iso: string,
  now: Date = NOW,
  timeZone = SHANGHAI,
  clockFormat?: "24h" | "12h",
): string {
  return formatUpcoming(iso, now, { timeZone, clockFormat });
}

describe("formatUpcoming day context (24-hour default)", () => {
  it("keeps a later-today instant as a bare clock time", () => {
    expect(upcoming("2026-10-17T17:30:00+08:00")).toBe("17:30");
  });

  it("labels tomorrow instead of showing a clock time alone (the reported case)", () => {
    expect(upcoming("2026-10-18T15:21:00+08:00")).toBe("Tomorrow 15:21");
  });

  it("calls 00:01 tomorrow when the local date changes, 20 minutes from now", () => {
    const now = new Date("2026-10-17T23:59:00+08:00");
    expect(upcoming("2026-10-18T00:01:00+08:00", now)).toBe("Tomorrow 00:01");
  });

  it("keeps today's clock for a target 23h59m away that stays on the same date", () => {
    const now = new Date("2026-10-18T00:00:00+08:00");
    expect(upcoming("2026-10-18T23:59:00+08:00", now)).toBe("23:59");
  });

  it("names the weekday two days out", () => {
    const now = new Date("2026-10-18T16:00:00+08:00");
    expect(upcoming("2026-10-20T15:21:00+08:00", now)).toBe("Tue 15:21");
  });

  it("still names the weekday at the tier's last day", () => {
    expect(upcoming("2026-10-23T09:05:00+08:00")).toBe("Fri 09:05");
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
    expect(upcoming("2026-03-08T20:30:00-04:00", now, NEW_YORK)).toBe("Tomorrow 20:30");
  });

  it("counts a fall-back day as one calendar day, not 25 hours", () => {
    const now = new Date("2026-11-01T00:30:00-04:00");
    expect(upcoming("2026-11-02T00:10:00-05:00", now, NEW_YORK)).toBe("Tomorrow 00:10");
  });

  it("keeps today's clock across a 24.5-hour fall-back day", () => {
    const now = new Date("2026-11-01T00:30:00-04:00");
    expect(upcoming("2026-11-01T23:30:00-05:00", now, NEW_YORK)).toBe("23:30");
  });
});

describe("formatUpcoming clock format", () => {
  it("renders AM/PM times when the viewer picked 12-hour", () => {
    expect(upcoming("2026-10-17T17:30:00+08:00", NOW, SHANGHAI, "12h")).toBe("5:30 PM");
    expect(upcoming("2026-10-18T15:21:00+08:00", NOW, SHANGHAI, "12h")).toBe("Tomorrow 3:21 PM");
  });

  it("renders midnight and noon as 12 AM / 12 PM in 12-hour mode", () => {
    const midnight = new Date("2026-10-17T23:50:00+08:00");
    expect(upcoming("2026-10-18T00:05:00+08:00", midnight, SHANGHAI, "12h")).toBe("Tomorrow 12:05 AM");
    expect(upcoming("2026-10-18T12:05:00+08:00", midnight, SHANGHAI, "12h")).toBe("Tomorrow 12:05 PM");
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
      "Tomorrow 15:21",
    );
  });

  it("judges the day boundary in the requested zone, not the process zone", () => {
    const now = new Date("2026-10-17T16:30:00Z");
    const target = "2026-10-18T00:15:00Z";
    expect(formatUpcoming(target, now, { timeZone: "UTC" })).toBe("Tomorrow 00:15");
    expect(formatUpcoming(target, now, { timeZone: SHANGHAI })).toBe("08:15");
  });
});

// meiva:note
// The suite pins TZ=UTC (tests/vitest.config.mts), so these timestamps render as UTC wall-clock values.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
const INSTANT = "2026-10-17T08:26:07.250Z";

describe("formatAbsolute clock format and zone name", () => {
  it("renders 24-hour text without a zone name by default", () => {
    expect(formatAbsolute(INSTANT)).toBe("2026-10-17 08:26:07");
  });

  it("adds the weekday between date and time when asked", () => {
    expect(formatAbsolute(INSTANT, { weekday: true })).toBe("2026-10-17 Sat 08:26:07");
  });

  it("appends the zone name only when showTimeZone is on", () => {
    expect(formatAbsolute(INSTANT, { showTimeZone: true })).toBe("2026-10-17 08:26:07 UTC");
  });

  it("renders a 12-hour clock when the viewer picked it", () => {
    expect(formatAbsolute(INSTANT, { clockFormat: "12h" })).toBe("2026-10-17 8:26:07 AM");
    expect(formatAbsolute(INSTANT, { clockFormat: "12h", showTimeZone: true })).toBe(
      "2026-10-17 8:26:07 AM UTC",
    );
  });

  it("returns an empty string for an unparseable iso", () => {
    expect(formatAbsolute("not-a-timestamp")).toBe("");
  });
});

describe("formatShort clock format", () => {
  it("renders compact 24-hour text by default", () => {
    expect(formatShort(INSTANT)).toBe("10/17 08:26");
  });

  it("renders a bare clock cell with includeDate: false", () => {
    expect(formatShort(INSTANT, { includeDate: false })).toBe("08:26");
  });

  it("renders a 12-hour clock when the viewer picked it", () => {
    expect(formatShort(INSTANT, { clockFormat: "12h" })).toBe("10/17 8:26 AM");
    expect(formatShort(INSTANT, { clockFormat: "12h", includeDate: false })).toBe("8:26 AM");
  });
});

describe("formatClockText", () => {
  it("renders 24-hour hours zero-padded", () => {
    expect(formatClockText(new Date(INSTANT))).toBe("08:26");
    expect(formatClockText(new Date("2026-10-17T00:05:00Z"))).toBe("00:05");
  });

  it("renders 12-hour hours without a leading zero and with a day period", () => {
    expect(formatClockText(new Date(INSTANT), { clockFormat: "12h" })).toBe("8:26 AM");
    expect(formatClockText(new Date("2026-10-17T00:05:00Z"), { clockFormat: "12h" })).toBe("12:05 AM");
    expect(formatClockText(new Date("2026-10-17T12:05:00Z"), { clockFormat: "12h" })).toBe("12:05 PM");
  });

  it("appends seconds and milliseconds on request", () => {
    const d = new Date(INSTANT);
    expect(formatClockText(d, { seconds: true })).toBe("08:26:07");
    expect(formatClockText(d, { milliseconds: true })).toBe("08:26:07.250");
    expect(formatClockText(d, { clockFormat: "12h", seconds: true })).toBe("8:26:07 AM");
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

  it("capitalises 'Just now' only when the value starts its own cell", () => {
    expect(formatRelative(NOW.toISOString(), NOW, { capitalize: true })).toBe("Just now");
    expect(formatRelative(NOW.toISOString(), NOW)).toBe("just now");
    // meiva:note
    // Above a minute the wording already starts with a digit, so the flag is a
    // no-op.
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    expect(formatRelative(new Date(NOW.getTime() - 300_000).toISOString(), NOW, { capitalize: true })).toBe("5m ago");
  });

  it("says 'just now' for a future instant still under a minute", () => {
    expect(formatRelative(new Date(NOW.getTime() + 30_000).toISOString(), NOW)).toBe("just now");
  });
});
