import type { Api } from "@/lib/transport/api";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { confirmedAnchor } from "./plan";
import { sameReadingIdentity } from "./identity";

const READING_REQUEST_BUDGET_MS = 7000;

// meiva:note
// One controller owns cancellation and the entire read chain's deadline.
// Aborting also settles callers whose test or transport ignores the signal.
// meiva:link ui/web/src/docs/frontend-state/timeline-cache.md
export function boundedReadingRequest<T>(controller: AbortController, read: (signal: AbortSignal) => Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  let cancel: () => void;
  const aborted = new Promise<never>((_resolve, reject) => {
    cancel = () => reject(new DOMException("Reading request cancelled", "AbortError"));
    if (controller.signal.aborted) cancel();
    else controller.signal.addEventListener("abort", cancel, { once: true });
    timer = setTimeout(() => {
      reject(new DOMException("Reading request deadline exceeded", "TimeoutError"));
      controller.abort();
    }, READING_REQUEST_BUDGET_MS);
  });
  const cleanup = () => {
    clearTimeout(timer);
    controller.signal.removeEventListener("abort", cancel);
  };
  return Promise.race([Promise.resolve().then(() => {
    controller.signal.throwIfAborted();
    return read(controller.signal);
  }), aborted]).then((value) => { cleanup(); return value; }, (error: unknown) => { cleanup(); throw error; });
}

export async function confirmLiveReadingCandidate(
  api: Pick<Api, "getTimeline" | "getReadingWindow">,
  agentId: number,
  candidate: BackendTimelineItem,
  signal: AbortSignal,
): Promise<BackendTimelineItem> {
  let locator = candidate.reading_locator;
  if (!locator) {
    const head = await api.getTimeline(agentId, { signal });
    signal.throwIfAborted();
    const matches = head.items.filter((item) => sameReadingIdentity(candidate, item) && item.reading_locator);
    if (matches.length !== 1) throw new Error("The current source has not confirmed this reading position");
    locator = matches[0].reading_locator;
  }
  if (!locator) throw new Error("The visible item has no restorable source identity");
  const page = await api.getReadingWindow(agentId, { anchor: locator }, signal, 1);
  signal.throwIfAborted();
  const item = confirmedAnchor(page);
  const hasStableId = (Boolean(candidate.source_message_id) || Boolean(candidate.source_inbound_id));
  if (hasStableId ? !sameReadingIdentity(candidate, item) : candidate.reading_locator !== item.reading_locator) {
    throw new Error("The source returned a different reading identity");
  }
  return item;
}
