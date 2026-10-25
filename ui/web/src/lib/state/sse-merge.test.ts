// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { USER_SETTING_DEFAULTS } from "../contracts/types";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import {
  MERGED_SSE_CHANNELS,
  SEPARATE_SSE_CHANNELS,
  SSE_SINGLE_STREAM_SETTING,
  mergedChannelsFor,
  useMergedSseChannels,
} from "./sse-merge";
import { SETTINGS_QUERY_KEY } from "./use-user-settings";

function settingsWrapper(overrides: Record<string, unknown> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(SETTINGS_QUERY_KEY, {
    ...USER_SETTING_DEFAULTS,
    ...overrides,
  });
  const api = cacheOnlyApi();
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      ApiProvider,
      { api },
      createElement(QueryClientProvider, { client }, children),
    );
  };
}

describe("merged SSE channel selection", () => {
  it("keeps every channel on its own endpoint while the switch is off", () => {
    const { result } = renderHook(() => useMergedSseChannels(), {
      wrapper: settingsWrapper(),
    });
    expect(result.current).toBe(SEPARATE_SSE_CHANNELS);
  });

  it("merges the whole channel set once the switch is on", () => {
    const { result } = renderHook(() => useMergedSseChannels(), {
      wrapper: settingsWrapper({ [SSE_SINGLE_STREAM_SETTING]: true }),
    });
    expect(result.current).toBe(MERGED_SSE_CHANNELS);
    expect([...MERGED_SSE_CHANNELS]).toEqual(["system", "systemAll", "alerts"]);
  });

  it("treats an empty channel set as no merge instead of a zero-channel stream", () => {
    expect(mergedChannelsFor(true, [])).toBe(SEPARATE_SSE_CHANNELS);
    expect(mergedChannelsFor(false, MERGED_SSE_CHANNELS)).toBe(SEPARATE_SSE_CHANNELS);
    expect(mergedChannelsFor(true, ["system"])).toEqual(["system"]);
  });
});
