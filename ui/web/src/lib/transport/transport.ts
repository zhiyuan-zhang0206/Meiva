// meiva:note
// Operation keys use randomUUID where the origin offers it; plain-HTTP origins
// have only getRandomValues.
export interface KeySource {
  readonly randomUUID: (() => string) | null;
  getRandomValues(bytes: Uint8Array): Uint8Array;
}

export type UploadRequest = Pick<
  XMLHttpRequest,
  "open" | "setRequestHeader" | "send" | "status" | "responseText" | "withCredentials" | "onload" | "onerror"
> & { upload: Pick<XMLHttpRequestUpload, "onprogress"> };

// meiva:note
// Gateway HTTP as the shells see it: one request, the monotonic clock that times
// it (read before and after each request), the upload channel and key entropy.
// system-transport.ts builds this from the real globals; tests build it from a
// real local server or a double that injects a fault the network cannot.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
export interface HttpTimers {
  readonly setTimeout: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  readonly clearTimeout: (handle: ReturnType<typeof setTimeout>) => void;
}

export interface HttpTransport {
  readonly fetch: (url: string, init: RequestInit) => Promise<Response>;
  now(): number;
  openUpload(): UploadRequest;
  readonly keys: KeySource;
  readonly timers?: HttpTimers;
}

export type EventSourceHandle = Pick<
  EventSource,
  "readyState" | "onopen" | "onmessage" | "onerror" | "close"
>;

export type ChannelHandle = Pick<BroadcastChannel, "onmessage" | "postMessage" | "close">;

export interface LockPort {
  request(
    name: string,
    options: LockOptions,
    callback: (lock: Lock | null) => Promise<void>,
  ): Promise<void>;
}

export type PageHandle = Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;

export interface Diagnostics {
  warn(message: string): void;
  error(message: string, cause: unknown): void;
}

export interface PageFacts {
  readonly secureContext: boolean;
  readonly locksObject: boolean;
}

// meiva:note
// The profile-shared event stream as the shells see it: sources, tab messaging,
// locks and the page. `openChannel` and `locks` are null where the platform has
// no BroadcastChannel or Web Locks, which selects the per-page fallback. The wall
// clock and the tab id only name a tab and stamp interest freshness.
// meiva:link ui/web/src/docs/frontend-data-flow/merged-stream.meiva.okf.md
export interface StreamTimers {
  readonly setTimeout: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  readonly clearTimeout: (handle: ReturnType<typeof setTimeout>) => void;
  readonly setInterval: (callback: () => void, ms: number) => ReturnType<typeof setInterval>;
  readonly clearInterval: (handle: ReturnType<typeof setInterval>) => void;
  readonly queueMicrotask: (callback: () => void) => void;
}

export interface StreamTransport {
  openEventSource(url: string, init: EventSourceInit): EventSourceHandle;
  readonly openChannel: ((name: string) => ChannelHandle) | null;
  readonly locks: LockPort | null;
  readonly page: PageHandle;
  wallNow(): number;
  newTabId(): string;
  readonly diagnostics: Diagnostics;
  readonly facts: PageFacts;
  readonly timers?: StreamTimers;
}

export interface SessionPort {
  checkAuth(): Promise<{ authenticated: boolean }>;
  sessionInvalid(): void;
}
