import type { ContentBlock } from "../../../contracts/types";
import { keyedHeaders } from "./key-plan";
import { hasHttpStatus, isAmbiguousDeliveryError } from "./response-plan";

const MESSAGE_POST_TIMEOUT_MS = 10_000;
const MESSAGE_RECONCILE_TIMEOUT_MS = 3_000;
const MESSAGE_RECONCILE_ATTEMPTS = 3;
const MESSAGE_RECONCILE_BASE_DELAY_MS = 500;

export class MessageDeliveryUnknownError extends Error {
  readonly clientMessageId: string;

  constructor(clientMessageId: string) {
    super(
      `Message delivery is still unconfirmed (id ${clientMessageId}); ` +
        "the draft has been kept.",
    );
    this.name = "MessageDeliveryUnknownError";
    this.clientMessageId = clientMessageId;
  }
}

export function messageInit(content: string | ContentBlock[], clientMessageId: string): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json", ...keyedHeaders(clientMessageId) },
    body: JSON.stringify({ content, source: "user" }),
  };
}

export type DeliveryState =
  | { stage: "start" }
  | { stage: "posted" }
  | { stage: "reconciling"; attempt: number; resent: boolean }
  | { stage: "resending"; attempt: number }
  | { stage: "waiting"; attempt: number; resent: boolean };

export type DeliveryEvent =
  | { kind: "begin" }
  | { kind: "succeeded" }
  | { kind: "failed"; error: unknown }
  | { kind: "waited" };

export type DeliveryAction =
  | { kind: "post"; timeoutMs: number }
  | { kind: "reconcile"; timeoutMs: number }
  | { kind: "wait"; ms: number }
  | { kind: "done" }
  | { kind: "fail"; error: unknown }
  | { kind: "unconfirmed" };

export interface DeliveryStep {
  state: DeliveryState;
  action: DeliveryAction;
}

const DONE: DeliveryAction = { kind: "done" };

function afterFailedAttempt(attempt: number, resent: boolean): DeliveryStep {
  if (attempt >= MESSAGE_RECONCILE_ATTEMPTS - 1) {
    return { state: { stage: "waiting", attempt, resent }, action: { kind: "unconfirmed" } };
  }
  return {
    state: { stage: "waiting", attempt, resent },
    action: { kind: "wait", ms: MESSAGE_RECONCILE_BASE_DELAY_MS * 2 ** attempt },
  };
}

function unexpected(state: DeliveryState, event: DeliveryEvent): Error {
  return new Error(`unexpected delivery event ${event.kind} in stage ${state.stage}`);
}

// meiva:note
// A send is one POST, then (only when its outcome is ambiguous) up to three
// reconcile reads keyed by the same client message id. A 404 from reconcile means
// the server never saw the message, so the original body is posted once more with
// the same id. A definite refusal ends the send at once; ambiguity that lasts
// through every attempt becomes an unconfirmed delivery the caller keeps a draft for.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
export function nextDeliveryStep(state: DeliveryState, event: DeliveryEvent): DeliveryStep {
  switch (state.stage) {
    case "start":
      if (event.kind !== "begin") throw unexpected(state, event);
      return { state: { stage: "posted" }, action: { kind: "post", timeoutMs: MESSAGE_POST_TIMEOUT_MS } };
    case "posted":
      if (event.kind === "succeeded") return { state, action: DONE };
      if (event.kind !== "failed") throw unexpected(state, event);
      if (!isAmbiguousDeliveryError(event.error)) return { state, action: { kind: "fail", error: event.error } };
      return {
        state: { stage: "reconciling", attempt: 0, resent: false },
        action: { kind: "reconcile", timeoutMs: MESSAGE_RECONCILE_TIMEOUT_MS },
      };
    case "reconciling":
      if (event.kind === "succeeded") return { state, action: DONE };
      if (event.kind !== "failed") throw unexpected(state, event);
      if (hasHttpStatus(event.error, 404)) {
        if (state.resent) return afterFailedAttempt(state.attempt, true);
        return {
          state: { stage: "resending", attempt: state.attempt },
          action: { kind: "post", timeoutMs: MESSAGE_POST_TIMEOUT_MS },
        };
      }
      if (!isAmbiguousDeliveryError(event.error)) return { state, action: { kind: "fail", error: event.error } };
      return afterFailedAttempt(state.attempt, state.resent);
    case "resending":
      if (event.kind === "succeeded") return { state, action: DONE };
      if (event.kind !== "failed") throw unexpected(state, event);
      if (!isAmbiguousDeliveryError(event.error)) return { state, action: { kind: "fail", error: event.error } };
      return afterFailedAttempt(state.attempt, true);
    case "waiting":
      if (event.kind !== "waited") throw unexpected(state, event);
      return {
        state: { stage: "reconciling", attempt: state.attempt + 1, resent: state.resent },
        action: { kind: "reconcile", timeoutMs: MESSAGE_RECONCILE_TIMEOUT_MS },
      };
  }
}
