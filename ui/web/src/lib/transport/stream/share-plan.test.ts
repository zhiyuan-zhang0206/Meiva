import { describe, expect, it } from "vitest";

import {
  detailUnion,
  mergedChannels,
  mergedStreamUrl,
  parseMergedFrame,
  staleTabs,
} from "./share-plan";

const ALL = new Set(["system", "systemAll", "alerts"] as const);

describe("merged stream url", () => {
  it("carries every merged channel on one source in wire order, with the idle agent filter", () => {
    const url = new URL(mergedStreamUrl("https://gw", ALL, ""));
    expect(url.origin + url.pathname).toBe("https://gw/api/stream");
    expect(url.searchParams.get("channels")).toBe("global,all,alerts");
    expect(url.searchParams.get("agents")).toBe("0");
  });

  it("names the union of interested agents once a detail feed is wanted", () => {
    expect(new URL(mergedStreamUrl("https://gw", ALL, "3,7")).searchParams.get("agents")).toBe("3,7");
  });

  it("omits the agent filter when the detail channel is not merged", () => {
    const url = new URL(mergedStreamUrl("https://gw", new Set(["alerts", "system"] as const), "3"));
    expect(url.searchParams.get("channels")).toBe("global,alerts");
    expect(url.searchParams.has("agents")).toBe(false);
  });

  it("orders merged channels system, systemAll, alerts regardless of insertion order", () => {
    expect(mergedChannels(new Set(["alerts", "system"] as const))).toEqual(["system", "alerts"]);
  });
});

describe("detail interest union", () => {
  it("is the sorted, de-duplicated set of selected agents", () => {
    expect(detailUnion([{ activeId: 7 }, { activeId: 3 }, { activeId: 7 }, { activeId: null }])).toBe("3,7");
  });

  it("sorts numerically, not lexically", () => {
    expect(detailUnion([{ activeId: 10 }, { activeId: 9 }])).toBe("9,10");
  });

  it("is empty when nobody has a selection", () => {
    expect(detailUnion([{ activeId: null }])).toBe("");
    expect(detailUnion([])).toBe("");
  });
});

describe("stale tabs", () => {
  it("lists tabs not seen since the cutoff, never the leader itself", () => {
    const interests = new Map([
      ["me", { seen: 0 }],
      ["old", { seen: 100 }],
      ["edge", { seen: 200 }],
      ["fresh", { seen: 201 }],
    ]);
    expect(staleTabs(interests, "me", 200)).toEqual(["old", "edge"]);
  });
});

describe("merged frame", () => {
  it("names its logical channel and carries its events", () => {
    expect(parseMergedFrame('{"ch":"global","events":[{"role":"label_updated","agent_id":3}]}')).toEqual({
      ch: "system",
      events: [{ role: "label_updated", agent_id: 3 }],
      dropped: 0,
    });
    expect(parseMergedFrame('{"ch":"all","events":[]}')?.ch).toBe("systemAll");
    expect(parseMergedFrame('{"ch":"alerts","events":[]}')?.ch).toBe("alerts");
  });

  it("reports the number of payloads the server shed", () => {
    expect(parseMergedFrame('{"ch":"all","events":[],"dropped":4}')?.dropped).toBe(4);
    expect(parseMergedFrame('{"ch":"all","events":[],"dropped":-2}')?.dropped).toBe(0);
    expect(parseMergedFrame('{"ch":"all","events":[],"dropped":"4"}')?.dropped).toBe(0);
  });

  it("degrades only the channel an error frame names", () => {
    expect(parseMergedFrame('{"ch":"all","error":"event stream interrupted: ConnectionError"}')).toEqual({
      ch: "systemAll",
      events: [],
      dropped: 0,
      error: "event stream interrupted: ConnectionError",
    });
  });

  it.each([
    "{not json",
    "[]",
    "null",
    '"text"',
    '{"events":[]}',
    '{"ch":7,"events":[]}',
    '{"ch":"unknown","events":[]}',
    '{"ch":"all"}',
    '{"ch":"all","events":"x"}',
  ])("cannot attribute %s to a channel", (raw) => {
    expect(parseMergedFrame(raw)).toBeNull();
  });
});
