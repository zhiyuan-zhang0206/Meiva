import { describe, expect, it } from "vitest";

import {
  messageInit,
  nextDeliveryStep,
  type DeliveryAction,
  type DeliveryEvent,
  type DeliveryState,
} from "./delivery-plan";
import { ApiError } from "./response-plan";

const POST: DeliveryAction = { kind: "post", timeoutMs: 10_000 };
const RECONCILE: DeliveryAction = { kind: "reconcile", timeoutMs: 3_000 };
const lost = { kind: "failed", error: new TypeError("response lost") } as const;
const missing = { kind: "failed", error: new ApiError(404, "not found") } as const;
const refused = { kind: "failed", error: new ApiError(409, "conflict") } as const;

function run(state: DeliveryState, ...events: DeliveryEvent[]) {
  let step: ReturnType<typeof nextDeliveryStep> = { state, action: POST };
  for (const event of events) step = nextDeliveryStep(step.state, event);
  return step;
}

describe("message request", () => {
  it("POSTs one stable client message id with the message", () => {
    expect(messageInit("hello", "client-message-123")).toEqual({
      method: "POST",
      headers: {
        "content-type": "application/json",
        "Idempotency-Key": "client-message-123",
        "Idempotency-Scope": "principal-v1",
      },
      body: '{"content":"hello","source":"user"}',
    });
  });

  it("refuses an unusable id before any request", () => {
    expect(() => messageInit("hello", "")).toThrow("idempotency key");
  });
});

describe("delivery planning", () => {
  it("starts with the original POST under its 10 second budget", () => {
    expect(nextDeliveryStep({ stage: "start" }, { kind: "begin" }).action).toEqual(POST);
  });

  it("finishes when the POST succeeds", () => {
    expect(run({ stage: "start" }, { kind: "begin" }, { kind: "succeeded" }).action).toEqual({ kind: "done" });
  });

  it("a definite refusal of the POST ends the send without reconciling", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, refused);
    expect(step.action).toEqual({ kind: "fail", error: refused.error });
  });

  it("an ambiguous POST failure reconciles by the same client message id", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, lost);
    expect(step.action).toEqual(RECONCILE);
    expect(step.state).toEqual({ stage: "reconciling", attempt: 0, resent: false });
  });

  it("a 408, 429 or 5xx POST is ambiguous like a lost response", () => {
    for (const status of [408, 429, 500, 503]) {
      const step = run({ stage: "start" }, { kind: "begin" }, { kind: "failed", error: new ApiError(status, "x") });
      expect(step.action).toEqual(RECONCILE);
    }
  });

  it("a reconcile that finds the message finishes the send", () => {
    expect(run({ stage: "start" }, { kind: "begin" }, lost, { kind: "succeeded" }).action).toEqual({ kind: "done" });
  });

  it("a missing receipt resends the original body once, with the same id", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, lost, missing);
    expect(step.action).toEqual(POST);
    expect(step.state).toEqual({ stage: "resending", attempt: 0 });
  });

  it("a resend that succeeds finishes the send", () => {
    expect(run({ stage: "start" }, { kind: "begin" }, lost, missing, { kind: "succeeded" }).action).toEqual({ kind: "done" });
  });

  it("a definitely refused resend ends the send", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, lost, missing, refused);
    expect(step.action).toEqual({ kind: "fail", error: refused.error });
  });

  it("an ambiguous resend keeps reconciling after a backoff", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, lost, missing, lost);
    expect(step.action).toEqual({ kind: "wait", ms: 500 });
    expect(step.state).toEqual({ stage: "waiting", attempt: 0, resent: true });
  });

  it("a definite refusal of a reconcile ends the send", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, lost, refused);
    expect(step.action).toEqual({ kind: "fail", error: refused.error });
  });

  it("waits 500 then 1000 milliseconds between the three reconcile attempts", () => {
    const first = run({ stage: "start" }, { kind: "begin" }, lost, lost);
    expect(first.action).toEqual({ kind: "wait", ms: 500 });
    const second = run(first.state, { kind: "waited" }, lost);
    expect(second.action).toEqual({ kind: "wait", ms: 1_000 });
    expect(nextDeliveryStep(second.state, { kind: "waited" }).action).toEqual(RECONCILE);
  });

  it("keeps a post-resend 404 ambiguous through the reconciliation deadline", () => {
    let step = run({ stage: "start" }, { kind: "begin" }, lost, missing, lost);
    const kinds: string[] = [step.action.kind];
    step = nextDeliveryStep(step.state, { kind: "waited" });
    kinds.push(step.action.kind);
    step = nextDeliveryStep(step.state, missing);
    kinds.push(step.action.kind);
    step = nextDeliveryStep(step.state, { kind: "waited" });
    kinds.push(step.action.kind);
    step = nextDeliveryStep(step.state, missing);
    kinds.push(step.action.kind);
    expect(kinds).toEqual(["wait", "reconcile", "wait", "reconcile", "unconfirmed"]);
  });

  it("the third ambiguous reconcile leaves the delivery unconfirmed", () => {
    const step = run({ stage: "start" }, { kind: "begin" }, lost, lost, { kind: "waited" }, lost, { kind: "waited" }, lost);
    expect(step.action).toEqual({ kind: "unconfirmed" });
  });

  it.each([
    [{ stage: "start" }, { kind: "succeeded" }],
    [{ stage: "posted" }, { kind: "waited" }],
    [{ stage: "reconciling", attempt: 0, resent: false }, { kind: "begin" }],
    [{ stage: "resending", attempt: 0 }, { kind: "waited" }],
    [{ stage: "waiting", attempt: 0, resent: false }, { kind: "succeeded" }],
  ] as const)("refuses the event that cannot follow stage %j", (state, event) => {
    expect(() => nextDeliveryStep(state, event)).toThrow("unexpected delivery event");
  });
});
