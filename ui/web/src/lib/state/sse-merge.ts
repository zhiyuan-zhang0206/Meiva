"use client";

import { useEffect } from "react";

import { useUserSettings } from "../state/use-user-settings";
import { registerSseRuntime } from "../transport/sse-lifecycle";
import type { SseChannel } from "../transport/stream/sse-share";
import {
  MERGED_SSE_CHANNELS,
  SSE_SINGLE_STREAM_SETTING,
  mergedChannelsFor,
} from "./sse-merge-plan";

export {
  MERGED_SSE_CHANNELS,
  SEPARATE_SSE_CHANNELS,
  SSE_SINGLE_STREAM_SETTING,
  mergedChannelsFor,
} from "./sse-merge-plan";

const warnState = {
  warnedEmptySelection: false,
};

export function useMergedSseChannels(): readonly SseChannel[] {
  const { settings } = useUserSettings();
  const on = settings[SSE_SINGLE_STREAM_SETTING] === true;
  // meiva:note
  // Switch on with an empty channel set is a half state: no source would carry
  // anything. It is reported once from an effect (rendering stays pure) and
  // treated as "do not merge" rather than silently becoming zero channels.
  // meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
  const halfState = on && MERGED_SSE_CHANNELS.length === 0;
  useEffect(() => {
    if (!halfState || warnState.warnedEmptySelection) return;
    warnState.warnedEmptySelection = true;
    console.warn(`[sse-merge] ${SSE_SINGLE_STREAM_SETTING} is on but no channels are merged`);
  }, [halfState]);
  return mergedChannelsFor(on, MERGED_SSE_CHANNELS);
}

registerSseRuntime({ useMergedChannels: useMergedSseChannels });
