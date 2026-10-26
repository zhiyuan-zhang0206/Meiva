import { operationKeyFrom } from "./plans";
import { errorFromResponse } from "./plans";
import { slowRequestReport, type SlowRequestReport } from "./plans";
import type { HttpTimers, HttpTransport, UploadRequest } from "../transport";

const defaultTimers: HttpTimers = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle),
};

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
  readonly accepted: (res: Response) => Promise<void>;
  readonly rawJson: <T>(res: Response) => Promise<T>;
  readonly decoded: <T>(res: Response, decode: (status: number, body: unknown) => T) => Promise<T>;
  readonly jsonTimed: <T>(
    path: string,
    init: RequestInit,
    timeoutMs: number,
    callerSignal?: AbortSignal,
    timeoutLabel?: string,
  ) => Promise<T>;
  readonly wait: (ms: number) => Promise<void>;
  readonly newOperationKey: () => string;
  readonly openUpload: () => UploadRequest;
}

function ok<T>(res: Response): Promise<T> {
  if (!res.ok) {
    return res.text().catch(() => "").then((text) => {
      throw errorFromResponse(res.status, text);
    });
  }
  return res.json() as Promise<T>;
}

// meiva:note
// Legacy endpoints expose raw HTTP text (including statusText fallback), while
// ordinary endpoints decode structured API errors through ok. Body ownership stays here.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
function accepted(res: Response): Promise<void> {
  if (res.ok) return Promise.resolve();
  return res.text().catch(() => "").then((text) => {
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  });
}

function rawJson<T>(res: Response): Promise<T> {
  return accepted(res).then(() => res.json());
}

function decoded<T>(res: Response, decode: (status: number, body: unknown) => T): Promise<T> {
  return ok<unknown>(res).then((body) => decode(res.status, body));
}

export function createHttp(transport: HttpTransport, base: string, telemetry: HttpTelemetry): Http {
  const timers = transport.timers ?? defaultTimers;
  const url = (path: string): string => `${base}${path}`;

  const send = (path: string, init?: RequestInit): Promise<Response> => {
    const startedAt = transport.now();
    const finish = (): void => {
      const report = slowRequestReport(path, transport.now() - startedAt);
      if (report !== null) telemetry.reportSlow(report);
    };
    return new Promise<Response>((resolve) => {
      resolve(transport.fetch(url(path), { ...init, credentials: "include" }));
    }).then(
      (res) => {
        finish();
        return res;
      },
      (err: unknown) => {
        finish();
        throw err;
      },
    );
  };

  const jsonTimed = <T>(
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
      timer = timers.setTimeout(() => {
        reject(new DOMException(`${timeoutLabel} exceeded ${timeoutMs}ms`, "TimeoutError"));
        controller.abort();
      }, timeoutMs);
    });
    const cleanup = (): void => {
      if (timer !== undefined) timers.clearTimeout(timer);
      callerSignal?.removeEventListener("abort", abortFromCaller);
    };
    return Promise.race([
      send(path, { ...init, signal: controller.signal }).then(ok<T>),
      timeout,
    ]).then(
      (res) => {
        cleanup();
        return res;
      },
      (err: unknown) => {
        cleanup();
        throw err;
      },
    );
  };

  return {
    url,
    send,
    ok,
    accepted,
    rawJson,
    decoded,
    jsonTimed,
    wait: (ms: number) =>
      new Promise<void>((resolve) => {
        timers.setTimeout(resolve, ms);
      }),
    newOperationKey: () => operationKeyFrom(transport.keys),
    openUpload: () => transport.openUpload(),
  };
}
