// meiva:note
// The timeout and caller cancellation remain connected through both response headers and response-body consumption. The Inspector client budget leaves serialization and network headroom above the backend work budget. HTTP rejection retains its status separately from network failure. Cross-origin gateway requests include session cookies. Slow-request telemetry removes query strings and normalizes numeric IDs, and telemetry requests do not report themselves.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
import { API_BASE, trackTransport } from "./api-base";
import { createControlApi } from "./api-control";
import type {
  NoticesFeed,
  AgentInspectStatistics,
  AgentInspectLive,
  InspectWidget,
  AgentMessageEnqueued,
  AgentRow,
  WireAgentRow,
  WireAgentRoster,
  WireAgentDirectoryPage,
  AgentRoster,
  AgentDirectoryPage,
  ConversationSnapshotResponse,
  ContextBreakdownResponse,
  CommandItem,
  ContentBlock,
  GuideDraftResponse,
  TaskListResponse,
  TaskFields,
  TaskRow,
  TaskSummaryRow,
  PendingInbound,
  RunTimelineContext,
  RunTimelineLinkContent,
  RunTimelineLinks,
  RunTimelineMessages,
  RunTimelineResponse,
  ShellCapture,
  TimelineResponse,
  TokenUsageResponse,
  ReplySuggestionResponse,
  UploadedBatch,
} from "../contracts/types";
import { projectAgentStatus } from "../contracts/types";
import { sendMessageWithReconciliation, MessageDeliveryUnknownError } from "./message-delivery";
import { submitUploadedFiles } from "./upload-delivery";
import { keyedHeaders, newOperationKey } from "./operation-key";

export { MessageDeliveryUnknownError };
export { API_BASE } from "./api-base";

export class ApiError extends Error {
  status: number;
  agentId?: number;
  code?: string;
  launchState?: { status: string; availability?: { reason: string; evidence_at?: string | null } | null };
  retryLaunchPath?: string;
  constructor(status: number, message: string, body?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    const detail = body?.detail;
    if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") {
      this.code = detail.code;
      if ("message" in detail && typeof detail.message === "string") this.message = detail.message;
    }
    if (body?.reason === "agent_launch_failed" && typeof body.agent_id === "number") {
      this.agentId = body.agent_id;
      if (typeof body.retry_launch_path === "string") this.retryLaunchPath = body.retry_launch_path;
      if (body.state && typeof body.state === "object") {
        this.launchState = body.state as ApiError["launchState"];
      }
    }
  }
}

// meiva:note
// A 422 body carries one `{msg}` object per rejected field; only that text is
// shown, so the operator reads the server's complaint rather than a JSON dump.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
function validationMessage(entry: unknown): string | null {
  if (typeof entry !== "object" || entry === null) return null;
  const msg = (entry as { msg?: unknown }).msg;
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
    for (const entry of detail as unknown[]) {
      const msg = validationMessage(entry);
      if (msg !== null) parts.push(msg);
    }
  }
  const reason = body?.reason;
  const joined = parts.length > 0 ? parts.join("; ") : `HTTP ${status}: ${text || "request failed"}`;
  return typeof reason === "string" && reason !== "" ? `${joined} (${reason})` : joined;
}

async function ok<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let body: Record<string, unknown> | undefined;
    try {
      const parsed: unknown = JSON.parse(text);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        body = parsed as Record<string, unknown>;
      }
    } catch { body = undefined; }
    throw new ApiError(res.status, serverMessage(res.status, body, text), body);
  }
  return res.json() as Promise<T>;
}

const url = (path: string): string => `${API_BASE}${path}`;

export const assetUrl = (path: string): string => `${API_BASE}${path}`;

function apiTimingKey(path: string): string {
  const pathname = path.split("?", 1)[0].replace(/^\/api\/?/, "");

  return pathname
    .split("/")
    .map((segment) => (/^\d+$/.test(segment) ? "id" : segment))
    .join(".");
}

async function f(path: string, init?: RequestInit): Promise<Response> {
  const startedAt = performance.now();
  try {
    return await fetch(url(path), { ...init, credentials: "include" });
  } finally {
    const durationMs = performance.now() - startedAt;
    if (durationMs > 800 && path.split("?", 1)[0] !== "/api/frontend-telemetry") {
      trackTransport("api-timing", {
        key: apiTimingKey(path),
        value: Math.round(durationMs),
        dedupe: false,
      });
    }
  }
}

async function jsonWithTimeout<T>(
  path: string,
  init: RequestInit,
  timeoutMs: number,
  callerSignal?: AbortSignal,
  timeoutLabel = "request",
): Promise<T> {
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (callerSignal?.aborted) controller.abort();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      reject(new DOMException(`${timeoutLabel} exceeded ${timeoutMs}ms`, "TimeoutError"));
      controller.abort();
    }, timeoutMs);
  });
  try {

    return await Promise.race([
      f(path, { ...init, signal: controller.signal }).then(ok<T>),
      timeout,
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

const INSPECT_REQUEST_TIMEOUT_MS = 35_000;

function isAmbiguousDeliveryError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

const POST: RequestInit = { method: "POST" };
const POST_JSON = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

function keyedJson(body: unknown, operationKey: string): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json", ...keyedHeaders(operationKey) },
    body: JSON.stringify(body),
  };
}

async function draftAgent(
  path: string,
  body: unknown,
  operationKey: string,
): Promise<GuideDraftResponse> {
  const init = keyedJson(body, operationKey);
  const response = await f(path, init);
  const accepted = await ok<unknown>(response);
  if (response.status !== 200 || !accepted || typeof accepted !== "object" ||
    !("agent_id" in accepted) || typeof accepted.agent_id !== "number" ||
    !Number.isSafeInteger(accepted.agent_id) || accepted.agent_id <= 0) {
    throw new Error("Draft agent acceptance is unconfirmed");
  }
  return accepted as GuideDraftResponse;
}
const PATCH_JSON = (body: unknown): RequestInit => ({
  method: "PATCH",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

const PUT_JSON = (body: unknown): RequestInit => ({
  method: "PUT",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  head: (path: string): Promise<Response> => f(path, { method: "HEAD" }),

  getTimeline: (
    agentId: number,
    opts?: { limit?: number; before?: string; signal?: AbortSignal },
  ): Promise<TimelineResponse> => {
    const params = new URLSearchParams();
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.before != null) params.set("before", opts.before);
    const qs = params.toString();
    return f(`/api/agents/${agentId}/timeline${qs ? `?${qs}` : ""}`, { signal: opts?.signal }).then(
      ok<TimelineResponse>,
    );
  },

  getTokenUsage: (agentId: number, signal?: AbortSignal): Promise<TokenUsageResponse> => {
    return f(`/api/agents/${agentId}/token-usage`, { signal }).then(ok<TokenUsageResponse>);
  },

  getReplySuggestion: (agentId: number, signal: AbortSignal): Promise<ReplySuggestionResponse> => {
    return f(`/api/agents/${agentId}/reply-suggestion`, { ...POST, signal }).then(
      ok<ReplySuggestionResponse>,
    );
  },

  getConversationSnapshot: (
    agentId: number,
    signal?: AbortSignal,
  ): Promise<ConversationSnapshotResponse> => {
    return f(`/api/agents/${agentId}/conversation-snapshot`, { signal }).then(
      ok<ConversationSnapshotResponse>,
    );
  },

  getContextBreakdown: (agentId: number): Promise<ContextBreakdownResponse> => {
    return f(`/api/agents/${agentId}/context-breakdown`).then(ok<ContextBreakdownResponse>);
  },

  getRunTimeline: (
    agentId: number,
    options?: { from?: string; to?: string; recentHours?: number; maxUnits?: number; snapshot?: string; signal?: AbortSignal },
  ): Promise<RunTimelineResponse> => {
    const params = new URLSearchParams();
    if (options?.from != null) params.set("from", options.from);
    if (options?.to != null) params.set("to", options.to);
    if (options?.recentHours != null) params.set("recent_hours", String(options.recentHours));
    if (options?.maxUnits != null) params.set("max_units", String(options.maxUnits));
    if (options?.snapshot != null) params.set("snapshot", options.snapshot);
    const query = params.toString();
    return jsonWithTimeout<RunTimelineResponse>(
      `/api/agents/${agentId}/run-timeline${query ? `?${query}` : ""}`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      options?.signal,
      "run timeline",
    );
  },

  getRunTimelineLinks: (
    agents: readonly number[],
    timeWindow: { from: string; to: string },
  ): Promise<RunTimelineLinks> => {
    const params = new URLSearchParams({ agents: agents.join(","), from: timeWindow.from, to: timeWindow.to });
    return f(`/api/insights/run-timeline/links?${params.toString()}`).then(ok<RunTimelineLinks>);
  },

  getRunTimelineLinkContent: (
    ref: { inbound_id: number } | { notice_id: number },
  ): Promise<RunTimelineLinkContent> => {
    const params = new URLSearchParams(Object.entries(ref).map(([k, v]) => [k, String(v)]));
    return f(`/api/insights/run-timeline/link-content?${params.toString()}`).then(ok<RunTimelineLinkContent>);
  },

  getRunTimelineContext: (agentId: number, at: number, snapshot?: string): Promise<RunTimelineContext> => {
    const params = new URLSearchParams({ at: String(at) });
    if (snapshot !== undefined) params.set("snapshot", snapshot);
    return f(`/api/agents/${agentId}/run-timeline/context?${params.toString()}`).then(ok<RunTimelineContext>);
  },

  getRunTimelineMessages: (
    agentId: number,
    range: { start: number; end: number; limit?: number; full?: boolean; snapshot?: string },
  ): Promise<RunTimelineMessages> => {
    const params = new URLSearchParams({ start: String(range.start), end: String(range.end) });
    if (range.limit != null) params.set("limit", String(range.limit));
    if (range.full) params.set("full", "true");
    if (range.snapshot !== undefined) params.set("snapshot", range.snapshot);
    return f(`/api/agents/${agentId}/run-timeline/messages?${params.toString()}`).then(
      ok<RunTimelineMessages>,
    );
  },

  getAgentInspectStatistics: (
    agentId: number,
    hours?: number | null,
    signal?: AbortSignal,
  ): Promise<AgentInspectStatistics> => {
    const params = new URLSearchParams();
    if (hours != null) params.set("hours", String(hours));
    const qs = params.toString();
    return jsonWithTimeout<AgentInspectStatistics>(
      `/api/agents/${agentId}/inspect/statistics${qs ? `?${qs}` : ""}`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      signal,
      "Inspector request",
    );
  },

  getAgentInspectLive: (
    agentId: number,
    signal?: AbortSignal,
  ): Promise<AgentInspectLive> => {
    return jsonWithTimeout<AgentInspectLive>(
      `/api/agents/${agentId}/inspect/live`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      signal,
      "Inspector live request",
    );
  },

  getAgentInspectWidgets: (
    agentId: number,
    signal?: AbortSignal,
  ): Promise<InspectWidget[]> => {
    return jsonWithTimeout<InspectWidget[]>(
      `/api/agents/${agentId}/inspect/widgets`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      signal,
      "Inspector widgets request",
    );
  },

  getAgentShell: (agentId: number, sessionId: number, lines?: number): Promise<ShellCapture> => {
    const qs = lines != null ? `?lines=${lines}` : "";
    return f(`/api/agents/${agentId}/shell/${sessionId}${qs}`).then(ok<ShellCapture>);
  },

  getPendingMessages: (agentId: number, signal?: AbortSignal): Promise<PendingInbound[]> => {
    return f(`/api/agents/${agentId}/pending`, { signal }).then(ok<PendingInbound[]>);
  },

  getCommands: (agentId?: number | null): Promise<CommandItem[]> => {
    const qs = agentId != null ? `?agent_id=${encodeURIComponent(agentId)}` : "";
    return f(`/api/commands${qs}`).then(ok<CommandItem[]>);
  },

  sendMessage: (
    agentId: number,
    content: string | ContentBlock[],
    clientMessageId: string,
  ): Promise<AgentMessageEnqueued> => {
    return sendMessageWithReconciliation(
      agentId,
      content,
      clientMessageId,
      jsonWithTimeout,
      isAmbiguousDeliveryError,
    );
  },

  getNotices: (params?: {
    limit?: number;
    resolvedLimit?: number;
    beforeAt?: string;
    beforeId?: number;
  }): Promise<NoticesFeed> => {
    const sp = new URLSearchParams();
    if (params?.limit != null) sp.set("limit", String(params.limit));
    if (params?.resolvedLimit != null) sp.set("resolved_limit", String(params.resolvedLimit));
    if (params?.beforeAt != null) sp.set("before_at", params.beforeAt);
    if (params?.beforeId != null) sp.set("before_id", String(params.beforeId));
    const qs = sp.toString();
    return f(`/api/notices${qs ? `?${qs}` : ""}`).then(ok<NoticesFeed>);
  },

  getAgentRoster: (signal?: AbortSignal): Promise<AgentRoster> =>
    f("/api/agents/roster", { signal }).then(ok<WireAgentRoster>).then((roster) => ({
      agents: roster.agents.map(projectAgentStatus), ancestors: roster.ancestors,
    })),

  listAgents: (options: { scope?: "live" | "terminated" | "all"; query?: string; beforeId?: number; limit?: number; signal?: AbortSignal } = {}): Promise<AgentDirectoryPage> => {
    const params = new URLSearchParams({ scope: options.scope ?? "live", limit: String(options.limit ?? 100) });
    if (options.query) params.set("query", options.query);
    if (options.beforeId != null) params.set("before_id", String(options.beforeId));
    return f(`/api/agents?${params}`, { signal: options.signal }).then(ok<WireAgentDirectoryPage>)
      .then((page) => ({ ...page, agents: page.agents.map(projectAgentStatus) }));
  },

  getAgent: (agentId: number, signal?: AbortSignal): Promise<AgentRow> =>
    f(`/api/agents/${agentId}`, { signal }).then(ok<WireAgentRow>).then(projectAgentStatus),

  getTasks: <T extends TaskFields = "full">(
    opts?: { window?: string; fields?: T },
  ): Promise<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>> => {
    const params = new URLSearchParams();
    if (opts?.window != null) params.set("window", opts.window);
    if (opts?.fields != null) params.set("fields", opts.fields);
    const qs = params.toString();
    return f(`/api/tasks${qs ? `?${qs}` : ""}`).then(
      ok<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>>,
    );
  },

  submitUploadedFiles,

  uploadFiles: (
    agentId: number,
    files: File[],
    onProgress?: (pct: number) => void,
    operationKey: string = newOperationKey(),
  ): Promise<UploadedBatch> => {
    const keyed = keyedHeaders(operationKey);
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file, file.name);
    }
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url(`/api/agents/${agentId}/uploads`));
      xhr.withCredentials = true;
      for (const [name, value] of Object.entries(keyed)) xhr.setRequestHeader(name, value);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText) as UploadedBatch);
        } else {
          let msg = `HTTP ${xhr.status}`;
          try { const err = JSON.parse(xhr.responseText) as { detail?: string }; if (err.detail) msg = err.detail; } catch { msg = `HTTP ${xhr.status}`; }
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error("Upload failed"));
      xhr.send(formData);
    });
  },

  ...createControlApi({
    f,
    ok,
    jsonWithTimeout,
    draftAgent,
    postInit: POST,
    postJson: POST_JSON,
    patchJson: PATCH_JSON,
    putJson: PUT_JSON,
    keyedJson,
  }),
};
