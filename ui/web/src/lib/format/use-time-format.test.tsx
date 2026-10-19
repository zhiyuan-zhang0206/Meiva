import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

import { useTimeFormat } from "./use-time-format";

const INSTANT = "2026-10-17T08:26:07.250Z";

beforeEach(() => resetMockSettings());

describe("useTimeFormat", () => {
  it("defaults to a 24-hour clock with no zone name", () => {
    const { result } = renderHook(() => useTimeFormat());
    expect(result.current.clockFormat).toBe("24h");
    expect(result.current.showTimeZone).toBe(false);
    expect(result.current.formatAbsolute(INSTANT)).toBe("2026-10-17 08:26:07");
    expect(result.current.formatShort(INSTANT)).toBe("10/17 08:26");
  });

  it("treats missing keys as the defaults (a viewer who never changed them)", () => {
    resetMockSettings({ "display.clock_format": undefined, "display.show_timezone": undefined });
    const { result } = renderHook(() => useTimeFormat());
    expect(result.current.clockFormat).toBe("24h");
    expect(result.current.showTimeZone).toBe(false);
    expect(result.current.formatUpcoming("2026-10-18T15:21:00Z", new Date("2026-10-17T16:26:00Z"))).toBe(
      "Tomorrow 15:21",
    );
  });

  it("follows the stored clock format and zone-name switch", () => {
    resetMockSettings({ "display.clock_format": "12h", "display.show_timezone": true });
    const { result } = renderHook(() => useTimeFormat());
    expect(result.current.clockFormat).toBe("12h");
    expect(result.current.showTimeZone).toBe(true);
    expect(result.current.formatAbsolute(INSTANT)).toBe("2026-10-17 8:26:07 AM UTC");
    expect(result.current.formatShort(INSTANT)).toBe("10/17 8:26 AM");
  });

  it("falls back to 24-hour for an unrecognized stored value", () => {
    resetMockSettings({ "display.clock_format": "13h" });
    const { result } = renderHook(() => useTimeFormat());
    expect(result.current.clockFormat).toBe("24h");
    expect(result.current.formatAbsolute(INSTANT)).toBe("2026-10-17 08:26:07");
  });

  it("re-renders when the setting changes", () => {
    const { result } = renderHook(() => useTimeFormat());
    act(() => setMockSetting("display.clock_format", "12h"));
    expect(result.current.clockFormat).toBe("12h");
    expect(result.current.formatShort(INSTANT, { includeDate: false })).toBe("8:26 AM");
  });
});
