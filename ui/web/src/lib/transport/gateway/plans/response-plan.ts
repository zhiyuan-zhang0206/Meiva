import type { GuideDraftResponse } from "../../../contracts/types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isLaunchState(
  value: unknown,
): value is NonNullable<ApiError["launchState"]> {
  return isRecord(value) && typeof value.status === "string";
}

function isGuideDraftResponse(value: unknown): value is GuideDraftResponse {
  return (
    isRecord(value) &&
    typeof value.agent_id === "number" &&
    Number.isSafeInteger(value.agent_id) &&
    value.agent_id > 0
  );
}

// meiva:note
// Retryability is what the server said about this exact request, never a guess from the
// status: a refusal that names a permanent cause must not reach a reader as "try again".
// A body without the field (a proxy page, an older server) falls back to the statuses
// that mean the same request may pass later.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
function retryableFrom(status: number, body: Record<string, unknown> | undefined): boolean {
  if (typeof body?.retryable === "boolean") return body.retryable;
  return status === 408 || status === 429 || status >= 500;
}

export class ApiError extends Error {
  status: number;
  agentId?: number;
  code?: string;
  retryable: boolean;
  launchState?: { status: string; availability?: { reason: string; evidence_at?: string | null } | null };
  retryLaunchPath?: string;
  constructor(status: number, message: string, body?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryable = retryableFrom(status, body);
    const detail = body?.detail;
    if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") {
      this.code = detail.code;
      if ("message" in detail && typeof detail.message === "string") this.message = detail.message;
    } else if (typeof body?.code === "string") {
      this.code = body.code;
    }
    if (body?.reason === "agent_launch_failed" && typeof body.agent_id === "number") {
      this.agentId = body.agent_id;
      if (typeof body.retry_launch_path === "string") this.retryLaunchPath = body.retry_launch_path;
      if (isLaunchState(body.state)) {
        this.launchState = body.state;
      }
    }
  }
}

// meiva:note
// A 422 body carries one `{msg}` object per rejected field; only that text is
// shown, so the operator reads the server's complaint rather than a JSON dump.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
function validationMessage(entry: unknown): string | null {
  if (!isRecord(entry)) return null;
  const msg = entry.msg;
  return typeof msg === "string" ? msg : null;
}

// meiva:note
// The error message is the server's own words, never a generic "failed": a string `detail` is shown as-is, a FastAPI validation list (422) is joined into its messages, and `reason` (the wire enum, e.g. `fork_harness_mismatch`) is appended when the server sent one - a spawn/fork refusal the operator can read instead of a swallowed failure.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
function serverMessage(status: number, body: Record<string, unknown> | undefined, text: string): string {
  const detail = body?.detail;
  const parts: string[] = [];
  if (typeof detail === "string") parts.push(detail);
  else if (Array.isArray(detail)) {
    for (const entry of detail) {
      const msg = validationMessage(entry);
      if (msg !== null) parts.push(msg);
    }
  }
  const reason = body?.reason;
  const joined = parts.length > 0 ? parts.join("; ") : `HTTP ${status}: ${text || "request failed"}`;
  return typeof reason === "string" && reason !== "" ? `${joined} (${reason})` : joined;
}

export function errorFromResponse(status: number, text: string): ApiError {
  let body: Record<string, unknown> | undefined;
  try {
    const parsed: unknown = JSON.parse(text);
    if (isRecord(parsed)) {
      body = parsed;
    }
  } catch { body = undefined; }
  return new ApiError(status, serverMessage(status, body, text), body);
}

export function confirmDraftReceipt(status: number, accepted: unknown): GuideDraftResponse {
  if (status !== 200 || !isGuideDraftResponse(accepted)) {
    throw new Error("Draft agent acceptance is unconfirmed");
  }
  return accepted;
}

export function isAmbiguousDeliveryError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

export function hasHttpStatus(error: unknown, status: number): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    error.status === status
  );
}
