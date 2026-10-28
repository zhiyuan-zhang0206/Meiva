// useInspectorOpen — the inspector open/closed flag is breakpoint-aware:
//
// - Desktop (≥ lg): a workspace preference (DB-backed display.inspector_open),
//   default CLOSED (the panel is a side panel beside the timeline — #1415).
// - Mobile (< lg): per-session view state (zustand, like the mobile sidebar
//   drawer), default CLOSED — the panel is a full-screen overlay that hides
//   the timeline, so first load must land on the timeline (task #793).
//   Mobile toggles NEVER write the shared setting: opening the inspector on a
//   phone must not yank the desktop panel open, and closing it there must not
//   close the desktop one.

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserProvider } from "../browser/browser-provider";
import { fakeBrowser } from "../browser/testing";
import { USER_SETTING_DEFAULTS } from "../contracts/types";
import { BREAKPOINT_LG_PX } from "../layout/breakpoint";
import { useStore } from "../state/store";
import { SETTINGS_QUERY_KEY } from "../state/use-user-settings";
import type { Api } from "../transport/api";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { useInspectorHours, useInspectorOpen } from "./inspector-panel-store";

const initialInnerWidth = window.innerWidth;

function resizeViewport(width: number): void {
  window.innerWidth = width;
  window.dispatchEvent(new Event("resize"));
}

const setSettingMock = vi.fn<(key: string, value: unknown) => void>();
let queryClient: QueryClient;
let storedSettings: Record<string, unknown>;

function setSettingValue(key: string, value: unknown): void {
  storedSettings = { ...storedSettings, [key]: value };
  queryClient.setQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY, (prev) => ({
    ...(prev ?? USER_SETTING_DEFAULTS),
    [key]: value,
  }));
}

const testApi: Api = {
  ...cacheOnlyApi(),
  getSettings: () =>
    Promise.resolve({
      settings: Object.entries(storedSettings)
        .filter(([, value]) => value !== undefined)
        .map(([key, value]) => ({
          key,
          value,
          updated_at: "2026-01-01T00:00:00Z",
        })),
    }),
  putSetting: (key, value) => {
    setSettingMock(key, value);
    storedSettings = { ...storedSettings, [key]: value };
    return Promise.resolve({
      key,
      value,
      updated_at: "2026-01-01T00:00:00Z",
    });
  },
};

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    BrowserProvider,
    { browser: fakeBrowser() },
    createElement(
      ApiProvider,
      { api: testApi },
      createElement(QueryClientProvider, { client: queryClient }, children),
    ),
  );
}

beforeEach(() => {
  resizeViewport(BREAKPOINT_LG_PX);
  setSettingMock.mockClear();
  storedSettings = { ...USER_SETTING_DEFAULTS };
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  queryClient.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS });
  useStore.setState({ mobileInspectorOpen: false, inspectorHours: 24 });
});

afterEach(() => {
  queryClient.clear();
  resizeViewport(initialInnerWidth);
  useStore.setState({ mobileInspectorOpen: false, inspectorHours: 24 });
});

describe("useInspectorOpen — desktop (≥ lg)", () => {
  it("defaults to CLOSED when the setting is unset (side panel, #1415)", () => {
    setSettingValue("display.inspector_open", undefined);
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    expect(result.current.open).toBe(false);
  });

  it("is open only when the setting is explicitly true", () => {
    setSettingValue("display.inspector_open", true);
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    expect(result.current.open).toBe(true);
  });

  it("toggle writes the shared setting and never touches the mobile store", async () => {
    setSettingValue("display.inspector_open", false);
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    expect(result.current.open).toBe(false);

    act(() => result.current.toggle());
    await waitFor(() => expect(result.current.open).toBe(true));
    expect(setSettingMock).toHaveBeenCalledWith("display.inspector_open", true);
    expect(useStore.getState().mobileInspectorOpen).toBe(false);

    act(() => result.current.toggle());
    await waitFor(() => expect(result.current.open).toBe(false));
    expect(setSettingMock).toHaveBeenLastCalledWith("display.inspector_open", false);
  });
});

describe("useInspectorHours", () => {
  it("defaults to 24h and updates the shared client selection", () => {
    const { result } = renderHook(() => useInspectorHours());
    expect(result.current.inspectorHours).toBe(24);
    act(() => result.current.setInspectorHours(1));
    expect(result.current.inspectorHours).toBe(1);
    expect(useStore.getState().inspectorHours).toBe(1);
  });
});

describe("useInspectorOpen — mobile (< lg)", () => {
  beforeEach(() => {
    resizeViewport(390);
  });

  it("defaults to closed — the overlay must not hide the timeline on first load", () => {
    // Even with the workspace setting at its default (open), mobile starts
    // on the timeline.
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    expect(result.current.open).toBe(false);
  });

  it("opens when toggled and closes when toggled again — session state only", () => {
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    expect(result.current.open).toBe(false);

    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);
    expect(useStore.getState().mobileInspectorOpen).toBe(true);

    act(() => result.current.toggle());
    expect(result.current.open).toBe(false);
    expect(useStore.getState().mobileInspectorOpen).toBe(false);
  });

  it("mobile toggle never writes the shared desktop setting", () => {
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    act(() => result.current.toggle());
    act(() => result.current.toggle());
    expect(setSettingMock).not.toHaveBeenCalled();
  });

  it("an explicit desktop preference does not force the overlay open on mobile", () => {
    // The workspace setting says open — but on mobile that only means the
    // desktop panel; the overlay stays closed until the user opens it here.
    setSettingValue("display.inspector_open", true);
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    expect(result.current.open).toBe(false);
  });
});

describe("useInspectorOpen — breakpoint switch", () => {
  it("flips between the two sources without cross-talk", () => {
    setSettingValue("display.inspector_open", false);
    const { result } = renderHook(() => useInspectorOpen(), { wrapper });
    // Desktop: closed (explicit false).
    expect(result.current.open).toBe(false);

    // → Mobile: session state (closed), desktop setting ignored.
    act(() => resizeViewport(390));
    expect(result.current.open).toBe(false);
    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);

    // → Desktop: back to the workspace setting, mobile session state ignored.
    act(() => resizeViewport(BREAKPOINT_LG_PX));
    expect(result.current.open).toBe(false);
  });
});
