import { afterEach, describe, expect, it, vi } from "vitest";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { httpRig, noUpload, sequentialKeys, UncancellableFetch } from "@/lib/transport/tests/api/api-provider";
import { boundedReadingRequest, confirmLiveReadingCandidate } from "./requests";

function item(overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return { item_id: "2.0", kind: "agent_chat", payload: "new complete reply", source: null, created_at: null,
    inbound_id: null, show_timestamp: true, source_message_id: "latest", source_block_idx: 0, ...overrides };
}

function rig(fetch: (...args: Parameters<typeof globalThis.fetch>) => Response | Promise<Response>) {
  return httpRig({ base: "http://reading.test", fetch: (...args) => Promise.resolve(fetch(...args)), now: () => 0, keys: sequentialKeys(), openUpload: noUpload });
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : "url" in input ? input.url : input.href;
}

function json(body: unknown): Response { return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }); }

afterEach(() => vi.useRealTimers());

describe("latest live item confirmation", () => {
  it("reads the current source, then verifies the canonical anchor with the same cancellation signal", async () => {
    const head = item({ reading_locator: "from_head" });
    const canonical = item({ item_id: "s1.cp.9.0", reading_locator: "canonical" });
    const transport = rig((url) => requestUrl(url).includes("/window?")
      ? json({ items: [canonical], anchor_item_id: canonical.item_id, reading_source: "real_domain" })
      : json({ items: [head], msg_count: 3, has_more: false }));
    const controller = new AbortController();
    await expect(boundedReadingRequest(controller, (signal) => confirmLiveReadingCandidate(transport.api, 7, item(), signal))).resolves.toEqual(canonical);
    expect(transport.sent.map((request) => request.url)).toEqual([
      "http://reading.test/api/agents/7/timeline",
      "http://reading.test/api/agents/7/timeline/window?anchor=from_head&limit=1",
    ]);
    expect(transport.sent.every((request) => request.init.signal === controller.signal)).toBe(true);
  });

  it("rejects the same display index with a different source identity before asking for an anchor", async () => {
    const transport = rig(() => json({ items: [item({ source_message_id: "wrong", reading_locator: "wrong" })] }));
    await expect(confirmLiveReadingCandidate(transport.api, 7, item(), new AbortController().signal)).rejects.toThrow("has not confirmed");
    expect(transport.sent).toHaveLength(1);
  });

  it("rejects an unqualified or incomplete anchor even when head metadata exists", async () => {
    const transport = rig((url) => requestUrl(url).includes("/window?")
      ? json({ items: [], anchor_item_id: null, reading_source: "domain" })
      : json({ items: [item({ reading_locator: "candidate" })] }));
    await expect(confirmLiveReadingCandidate(transport.api, 7, item(), new AbortController().signal)).rejects.toThrow("canonical anchor");
  });

  it("revalidates an existing locator without another head read", async () => {
    const bound = item({ reading_locator: "known" });
    const transport = rig(() => json({ items: [bound], anchor_item_id: bound.item_id, reading_source: "domain" }));
    await expect(confirmLiveReadingCandidate(transport.api, 7, bound, new AbortController().signal)).resolves.toEqual(bound);
    expect(transport.sent.map((request) => request.url)).toEqual(["http://reading.test/api/agents/7/timeline/window?anchor=known&limit=1"]);
  });

  it("settles cancellation when the transport ignores it and ignores its late completion", async () => {
    const late = new UncancellableFetch();
    const transport = rig(late.fetch);
    const controller = new AbortController();
    const read = boundedReadingRequest(controller, (signal) => confirmLiveReadingCandidate(transport.api, 7, item(), signal));
    const rejected = expect(read).rejects.toMatchObject({ name: "AbortError" });
    await Promise.resolve();
    controller.abort();
    await rejected;
    late.respond({ items: [item({ reading_locator: "late" })] });
    await Promise.resolve(); await Promise.resolve();
    expect(transport.sent).toHaveLength(1);
  });

  it("bounds the whole chain and aborts a response that never arrives", async () => {
    vi.useFakeTimers();
    const transport = rig(() => new Promise<Response>(() => undefined));
    const controller = new AbortController();
    const read = boundedReadingRequest(controller, (signal) => confirmLiveReadingCandidate(transport.api, 7, item(), signal));
    const rejected = expect(read).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(7000);
    await rejected;
    expect(controller.signal.aborted).toBe(true);
    expect(transport.sent).toHaveLength(1);
  });
});
