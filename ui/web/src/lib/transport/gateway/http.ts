import { operationKeyFrom } from "./plans";
import { errorFromResponse } from "./plans";
import { slowRequestReport, type SlowRequestReport } from "./plans";
import type { HttpTransport, UploadRequest } from "../transport";

export interface HttpTelemetry {
  reportSlow(report: SlowRequestReport): void;
}

// meiva:note
// The gateway client every endpoint is written against. `send` is one credentialed
// request timed on the transport's monotonic clock; a slow one is reported before
// its outcome is returned or rethrown. `jsonTimed` keeps the deadline and the
// caller's cancellation connected through both the headers and the body, and a
// deadline rejects with a TimeoutError. Cross-origin gateway requests carry the
// session cookie because every request is sent with credentials included.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
export interface Http {
  readonly url: (path: string) => string;
  readonly send: (path: string, init?: RequestInit) => Promise<Response>;
  readonly ok: <T>(res: Response) => Promise<T>;
  readonly jsonTimed: <T>(
    path: string,
    init: RequestInit,
    timeoutMs: number,
    callerSignal?: AbortSignal,
    timeoutLabel?: string,
  ) => Promise<T>;
  readonly newOperationKey: () => string;
  readonly openUpload: () => UploadRequest;
}

async function ok<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw errorFromResponse(res.status, text);
  }
  return res.json() as Promise<T>;
}

export function createHttp(transport: HttpTransport, base: string, telemetry: HttpTelemetry): Http {
  const url = (path: string): string => `${base}${path}`;

  const send = async (path: string, init?: RequestInit): Promise<Response> => {
    const startedAt = transport.now();
    try {
      return await transport.fetch(url(path), { ...init, credentials: "include" });
    } finally {
      const report = slowRequestReport(path, transport.now() - startedAt);
      if (report !== null) telemetry.reportSlow(report);
    }
  };

  const jsonTimed = async <T>(
    path: string,
    init: RequestInit,
    timeoutMs: number,
    callerSignal?: AbortSignal,
    timeoutLabel = "request",
  ): Promise<T> => {
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
        send(path, { ...init, signal: controller.signal }).then(ok<T>),
        timeout,
      ]);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      callerSignal?.removeEventListener("abort", abortFromCaller);
    }
  };

  return {
    url,
    send,
    ok,
    jsonTimed,
    newOperationKey: () => operationKeyFrom(transport.keys),
    openUpload: () => transport.openUpload(),
  };
}
