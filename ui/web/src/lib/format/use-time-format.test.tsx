// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { USER_SETTING_DEFAULTS } from "../contracts/types";
import { SETTINGS_QUERY_KEY } from "../state/use-user-settings";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { useTimeFormat } from "./use-time-format";

const INSTANT = "2026-10-17T08:26:07.250Z";
process.env.TZ = "UTC";

function createSettingsRig(initial: Record<string, unknown> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(SETTINGS_QUERY_KEY, {
    ...USER_SETTING_DEFAULTS,
    ...initial,
  });
  const api = cacheOnlyApi();
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(
      ApiProvider,
      { api },
      createElement(QueryClientProvider, { client }, children),
    );
  return {
    wrapper,
    setSetting: (key: string, value: unknown) => {
      client.setQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY, (old) => ({
        ...(old ?? USER_SETTING_DEFAULTS),
        [key]: value,
      }));
    },
  };
}

describe("useTimeFormat", () => {
  it("defaults to a 24-hour clock with no zone name", () => {
    const { wrapper } = createSettingsRig();
    const { result } = renderHook(() => useTimeFormat(), { wrapper });
    expect(result.current.clockFormat).toBe("24h");
    expect(result.current.formatAbsolute(INSTANT)).toBe("2026-10-17 08:26:07");
    expect(result.current.formatShort(INSTANT)).toBe("10/17 08:26");
  });

  it("treats missing keys as the defaults (a viewer who never changed them)", () => {
    const { wrapper } = createSettingsRig({
      "display.clock_format": undefined,
    });
    const { result } = renderHook(() => useTimeFormat(), { wrapper });
    expect(result.current.clockFormat).toBe("24h");
    expect(
      result.current.formatUpcoming(
        "2026-10-18T15:21:00Z",
        new Date("2026-10-17T16:26:00Z"),
      ),
    ).toBe("Tomorrow 15:21");
  });

  it("follows the stored clock format", () => {
    const { wrapper } = createSettingsRig({
      "display.clock_format": "12h",
    });
    const { result } = renderHook(() => useTimeFormat(), { wrapper });
    expect(result.current.clockFormat).toBe("12h");
    expect(result.current.formatAbsolute(INSTANT)).toBe("2026-10-17 8:26:07 AM");
    expect(result.current.formatShort(INSTANT)).toBe("10/17 8:26 AM");
  });

  it("falls back to 24-hour for an unrecognized stored value", () => {
    const { wrapper } = createSettingsRig({ "display.clock_format": "13h" });
    const { result } = renderHook(() => useTimeFormat(), { wrapper });
    expect(result.current.clockFormat).toBe("24h");
    expect(result.current.formatAbsolute(INSTANT)).toBe("2026-10-17 08:26:07");
  });

  it("re-renders when the setting changes", async () => {
    const { wrapper, setSetting } = createSettingsRig();
    const { result } = renderHook(() => useTimeFormat(), { wrapper });
    act(() => setSetting("display.clock_format", "12h"));
    await waitFor(() => {
      expect(result.current.clockFormat).toBe("12h");
    });
    expect(result.current.formatShort(INSTANT, { includeDate: false })).toBe("8:26 AM");
  });
});
