"use client";

import { useEffect } from "react";

import { useUserSettings } from "../state/use-user-settings";
import type { SseChannel } from "../transport/sse-share";

// meiva:note
// Which logical channels ride the one merged `/api/stream` source, and the
// setting that decides whether they ride it at all (off is the shipped default:
// every provider keeps its own endpoint, byte for byte). The set is the rollback
// lever: an empty set (or a single channel) returns the browser to
// the per-channel endpoints without touching the switch or the gateway. The
// setting below only decides whether this set is used.
// meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
export const MERGED_SSE_CHANNELS: readonly SseChannel[] = ["system", "systemAll", "alerts"];

export const SEPARATE_SSE_CHANNELS: readonly SseChannel[] = [];

export const SSE_SINGLE_STREAM_SETTING = "web.sseSingleStream";

let warnedEmptySelection = false;

export function mergedChannelsFor(
  singleStream: boolean,
  channels: readonly SseChannel[],
): readonly SseChannel[] {
  if (!singleStream || channels.length === 0) return SEPARATE_SSE_CHANNELS;
  return channels;
}

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
    if (!halfState || warnedEmptySelection) return;
    warnedEmptySelection = true;
    console.warn(`[sse-merge] ${SSE_SINGLE_STREAM_SETTING} is on but no channels are merged`);
  }, [halfState]);
  return mergedChannelsFor(on, MERGED_SSE_CHANNELS);
}
