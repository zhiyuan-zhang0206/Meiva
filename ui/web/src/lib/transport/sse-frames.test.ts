import { describe, expect, it } from "vitest";

import { classifySystemFrame, nextParseFailures, selectedAgentEvents } from "./sse-frames";

describe("system frame", () => {
  it("heartbeat frame is liveness-only", () => {
    expect(classifySystemFrame('{"role":"heartbeat"}')).toEqual({ kind: "heartbeat" });
  });

  it("a single event frame is a one-event batch", () => {
    expect(classifySystemFrame('{"role":"llm_done","agent_id":7}')).toEqual({
      kind: "events",
      events: [{ role: "llm_done", agent_id: 7 }],
    });
  });

  it("a batch frame keeps its object members in order", () => {
    const frame = classifySystemFrame(JSON.stringify([
      { role: "chat_delta", agent_id: 7, content: "a" },
      { role: "agent_updated", agent_id: 42 },
    ]));
    expect(frame).toEqual({
      kind: "events",
      events: [{ role: "chat_delta", agent_id: 7, content: "a" }, { role: "agent_updated", agent_id: 42 }],
    });
  });

  it("non-object elements in a batch frame are dropped, not fanned out", () => {
    const frame = classifySystemFrame(JSON.stringify(["corrupt-string-element", 42, null, { role: "agent_updated", agent_id: 42 }]));
    expect(frame).toEqual({ kind: "events", events: [{ role: "agent_updated", agent_id: 42 }] });
  });

  it("a heartbeat inside a batch is skipped and the rest is kept", () => {
    const frame = classifySystemFrame(JSON.stringify([
      { role: "chat_delta", agent_id: 7, content: "a" },
      { role: "heartbeat" },
      { role: "chat_delta", agent_id: 7, content: "b" },
    ]));
    expect(frame.kind === "events" && frame.events.map((event) => (event as { content: string }).content)).toEqual(["a", "b"]);
  });

  it("malformed JSON is reported with the parse error", () => {
    const frame = classifySystemFrame("{not valid json");
    expect(frame.kind).toBe("malformed");
    expect(frame.kind === "malformed" && frame.error).toBeInstanceOf(SyntaxError);
  });

  it("a non-string payload placeholder is malformed, not a crash", () => {
    expect(classifySystemFrame("[non-string SSE payload]").kind).toBe("malformed");
  });
});

describe("detail stream selection", () => {
  const events = [
    { role: "chat_delta", agent_id: 7 },
    { role: "cluster_status", agent_id: 0 },
    { role: "chat_delta", agent_id: 8 },
  ] as never;

  it("keeps the selected agent's events and system-level events", () => {
    expect(selectedAgentEvents(events, 7)).toEqual([
      { role: "chat_delta", agent_id: 7 },
      { role: "cluster_status", agent_id: 0 },
    ]);
  });

  it("keeps nothing when no agent is selected", () => {
    expect(selectedAgentEvents(events, null)).toEqual([]);
  });
});

describe("parse failure count", () => {
  it("forces a reopen on the third consecutive corrupt frame and starts over", () => {
    expect(nextParseFailures(0)).toEqual({ failures: 1, reopen: false });
    expect(nextParseFailures(1)).toEqual({ failures: 2, reopen: false });
    expect(nextParseFailures(2)).toEqual({ failures: 0, reopen: true });
  });
});
