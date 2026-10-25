import type { SseChannel } from "../transport/stream/sse-share";

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

export function mergedChannelsFor(
  singleStream: boolean,
  channels: readonly SseChannel[],
): readonly SseChannel[] {
  if (!singleStream || channels.length === 0) return SEPARATE_SSE_CHANNELS;
  return channels;
}
