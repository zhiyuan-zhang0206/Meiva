import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetMockSettings } from "@/test-support/user-settings-mock";

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

import {
  MERGED_SSE_CHANNELS,
  SEPARATE_SSE_CHANNELS,
  SSE_SINGLE_STREAM_SETTING,
  mergedChannelsFor,
  useMergedSseChannels,
} from "./sse-merge";

describe("merged SSE channel selection", () => {
  beforeEach(() => {
    resetMockSettings();
  });

  it("keeps every channel on its own endpoint while the switch is off", () => {
    const { result } = renderHook(() => useMergedSseChannels());
    expect(result.current).toBe(SEPARATE_SSE_CHANNELS);
  });

  it("merges the whole channel set once the switch is on", () => {
    resetMockSettings({ [SSE_SINGLE_STREAM_SETTING]: true });
    const { result } = renderHook(() => useMergedSseChannels());
    expect(result.current).toBe(MERGED_SSE_CHANNELS);
    expect([...MERGED_SSE_CHANNELS]).toEqual(["system", "systemAll", "alerts"]);
  });

  it("treats an empty channel set as no merge instead of a zero-channel stream", () => {
    expect(mergedChannelsFor(true, [])).toBe(SEPARATE_SSE_CHANNELS);
    expect(mergedChannelsFor(false, MERGED_SSE_CHANNELS)).toBe(SEPARATE_SSE_CHANNELS);
    expect(mergedChannelsFor(true, ["system"])).toEqual(["system"]);
  });
});
