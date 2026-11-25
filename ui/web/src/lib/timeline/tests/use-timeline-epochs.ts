import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TimelineResponse } from "../../contracts/types";
import { useTimelineStore } from "../timeline-store";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineEpochTests(ctx: UseTimelineTestContext): void {
  const {
    timeline,
    snapshot,
    timelineReads,
    snapshotReads,
    resolves,
    snapshotItem,
    tlResp,
    composedSnapshot,
    wrapper,
  } = ctx;

  const switchBlock = (seq: number, payload: string) =>
    snapshotItem({
      item_id: `e${seq}.sw`,
      kind: "system_marker",
      source: "harness_switch",
      payload,
    });

describe("useTimeline across harness epochs", () => {
  it("replays the gateway's recorded pages through paging in the gateway's own order", async () => {
    const dir = join(__dirname, "epoch-pages");
    const pages = readdirSync(dir).sort().map((name) => JSON.parse(readFileSync(join(dir, name), "utf8")) as {
      request: string;
      response: TimelineResponse;
    });
    timeline.queue(...pages.map((page) => resolves(page.response)));
    const showError = vi.fn();

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items.length).toBeGreaterThan(0));
    timelineReads.length = 0;
    for (let i = 1; i < pages.length; i++) {
      const before = result.current.items.length;
      act(() => result.current.loadOlder("reader"));
      await waitFor(() => expect(result.current.items.length).toBeGreaterThan(before));
    }

    expect(timelineReads.map((read) => read.before)).toEqual(["e3.sw", "e2.sw", "e1.sw", "e0.6.0"]);
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "e0.s1.cp7.3.0",
      "e0.s1.cp7.4.0",
      "e0.0.0",
      "e0.6.0",
      "e0.7.0",
      "e1.sw",
      "e2.sw",
      "e2.h900.0",
      "e2.h1800.0",
      "e3.sw",
      "40.0",
      "41.0",
      "41.1",
    ]);
    expect(result.current.hasMoreOlder).toBe(false);
    expect(useTimelineStore.getState().currentEpoch).toBe(3);
    expect(showError).not.toHaveBeenCalled();
  });

  it("pages from the current epoch back through meiva -> claude_code -> meiva in one order", async () => {
    const showError = vi.fn();
    timeline.queue(
      resolves(tlResp([
        snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
        snapshotItem({ item_id: "2.0", payload: "epoch 2 reply" }),
      ], true)),
      resolves(tlResp([
        snapshotItem({ item_id: "e1.h100.0", payload: "claude reply a" }),
        snapshotItem({ item_id: "e1.h200.0", payload: "claude reply b" }),
        switchBlock(2, "Switched harness: claude_code -> meiva"),
      ], true)),
      resolves(tlResp([
        snapshotItem({ item_id: "e0.1.0", payload: "epoch 0 reply" }),
        switchBlock(1, "Switched harness: meiva -> claude_code"),
      ], false)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    timelineReads.length = 0;

    act(() => result.current.loadOlder("reader"));
    await waitFor(() => expect(result.current.items).toHaveLength(5));
    expect(result.current.hasMoreOlder).toBe(true);

    act(() => result.current.loadOlder("reader"));
    await waitFor(() => expect(result.current.items).toHaveLength(7));

    expect(timelineReads.map((read) => read.before)).toEqual(["2.0", "e1.h100.0"]);
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "e0.1.0",
      "e1.sw",
      "e1.h100.0",
      "e1.h200.0",
      "e2.sw",
      "0.0",
      "2.0",
    ]);
    expect(result.current.hasMoreOlder).toBe(false);
    expect(showError).not.toHaveBeenCalled();
  });

  it("continues past a synthetic row when it is the oldest loaded item", async () => {
    const showError = vi.fn();
    timeline.queue(
      resolves(tlResp([
        switchBlock(1, "Switched harness: meiva -> claude_code"),
        snapshotItem({ item_id: "h100.0", payload: "claude reply" }),
      ], true)),
      resolves(tlResp([snapshotItem({ item_id: "e0.3.0", payload: "meiva reply" })], false)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    timelineReads.length = 0;

    act(() => result.current.loadOlder("reader"));
    await waitFor(() => expect(result.current.items).toHaveLength(3));

    expect(timelineReads.map((read) => read.before)).toEqual(["e1.sw"]);
    expect(result.current.items.map((i) => i.item_id)).toEqual(["e0.3.0", "e1.sw", "h100.0"]);
  });

  it("a harness change drops the old epoch's rows and rereads the new head", async () => {
    const showError = vi.fn();
    timeline.queue(resolves(tlResp([
      snapshotItem({ item_id: "1.0", payload: "native one" }),
      snapshotItem({ item_id: "2.0", payload: "native two" }),
    ])));
    snapshot.always(resolves(composedSnapshot(tlResp([
      snapshotItem({ item_id: "h300.0", payload: "claude one" }),
    ]))));

    const { result, rerender } = renderHook(
      ({ harness }: { harness: string }) => useTimeline(42, showError, "idle", harness),
      { initialProps: { harness: "meiva" }, wrapper },
    );
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(snapshotReads).toHaveLength(0);

    rerender({ harness: "claude_code" });

    await waitFor(() =>
      expect(result.current.items.map((i) => i.item_id)).toEqual(["h300.0"]),
    );
    expect(snapshotReads).toHaveLength(1);
    expect(showError).not.toHaveBeenCalled();
  });

  it("an unchanged harness, or its first appearance, keeps the view", async () => {
    const showError = vi.fn();
    timeline.queue(resolves(tlResp([snapshotItem({ item_id: "1.0", payload: "native one" })])));

    const { result, rerender } = renderHook(
      ({ harness }: { harness: string | undefined }) => useTimeline(42, showError, "idle", harness),
      { initialProps: { harness: undefined as string | undefined }, wrapper },
    );
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    rerender({ harness: "meiva" });
    rerender({ harness: "meiva" });

    await ctx.delay(20);
    expect(result.current.items.map((i) => i.item_id)).toEqual(["1.0"]);
    expect(snapshotReads).toHaveLength(0);
  });
});
}
