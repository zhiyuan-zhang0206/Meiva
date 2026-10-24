import type { ChannelHandle, Diagnostics, EventSourceHandle, LockPort, StreamTransport } from "../../transport";

export const READY = { CONNECTING: 0, OPEN: 1, CLOSED: 2 } as const;

export class FakeEventSource implements EventSourceHandle {
  readyState: number = READY.CONNECTING;
  onopen: EventSourceHandle["onopen"] = null;
  onmessage: EventSourceHandle["onmessage"] = null;
  onerror: EventSourceHandle["onerror"] = null;

  constructor(readonly url: string, readonly init: EventSourceInit) {}

  close(): void {
    this.readyState = READY.CLOSED;
  }

  open(): void {
    this.readyState = READY.OPEN;
    this.onopen?.call(this as unknown as EventSource, new Event("open"));
  }

  frame(data: unknown): void {
    this.onmessage?.call(this as unknown as EventSource, new MessageEvent("message", { data }));
  }

  error(state: number): void {
    this.readyState = state;
    this.onerror?.call(this as unknown as EventSource, new Event("error"));
  }
}

class FakeChannel implements ChannelHandle {
  onmessage: ChannelHandle["onmessage"] = null;

  constructor(private readonly peers: Set<FakeChannel>) {
    peers.add(this);
  }

  postMessage(data: unknown): void {
    for (const peer of this.peers) {
      if (peer !== this) peer.onmessage?.call(peer as unknown as BroadcastChannel, { data } as MessageEvent<unknown>);
    }
  }

  close(): void {
    this.peers.delete(this);
  }
}

interface QueuedLock {
  callback: (lock: Lock | null) => Promise<void>;
  signal: AbortSignal;
  resolve: () => void;
  reject: (error: unknown) => void;
}

class FakeLocks implements LockPort {
  failure: Error | null = null;
  private held = false;
  private queue: QueuedLock[] = [];

  request(name: string, options: LockOptions, callback: (lock: Lock | null) => Promise<void>): Promise<void> {
    if (this.failure !== null) return Promise.reject(this.failure);
    if (name !== "meiva-ui-sse" || options.mode !== "exclusive") {
      return Promise.reject(new Error(`unexpected lock request ${name} ${String(options.mode)}`));
    }
    const signal = options.signal;
    if (!signal) return Promise.reject(new Error("missing lock abort signal"));
    return new Promise<void>((resolve, reject) => {
      const item = { callback, signal, resolve, reject };
      this.queue.push(item);
      signal.addEventListener("abort", () => {
        if (!this.queue.includes(item)) return;
        this.queue = this.queue.filter((queued) => queued !== item);
        reject(new DOMException("aborted", "AbortError"));
      });
      this.pump();
    });
  }

  private pump(): void {
    if (this.held) return;
    const item = this.queue.shift();
    if (!item) return;
    this.held = true;
    void item.callback({ name: "meiva-ui-sse", mode: "exclusive" })
      .then(item.resolve, item.reject)
      .finally(() => {
        this.held = false;
        this.pump();
      });
  }
}

class FakePage extends EventTarget {
  visibilityState: DocumentVisibilityState = "visible";

  show(visible: boolean): void {
    this.visibilityState = visible ? "visible" : "hidden";
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

export class FakeNetwork {
  readonly channels = new Set<FakeChannel>();
  readonly locks = new FakeLocks();
  private tabs = 0;

  nextTabId(): string {
    this.tabs += 1;
    return `tab-${this.tabs}`;
  }
}

export type TabCapability = "shared" | "legacy";

export interface FakeTabConfig {
  network: FakeNetwork;
  capability: TabCapability;
  wallNow: () => number;
  secureContext: boolean;
}

// meiva:note
// One browser document as the shared event stream sees it: a page whose
// visibility the test flips, event sources it can drive by hand (the platform's
// EventSource state machine cannot be produced by a real server on demand), the
// BroadcastChannel and Web Locks of the profile it shares with other tabs, and a
// diagnostics sink that keeps what the transport reported. A `legacy` tab has
// neither channels nor locks, which selects the per-page fallback.
export class FakeTab {
  readonly page = new FakePage();
  readonly sources: FakeEventSource[] = [];
  readonly channels: FakeChannel[] = [];
  readonly warnings: string[] = [];
  readonly errors: { message: string; cause: unknown }[] = [];
  readonly transport: StreamTransport;
  private failOnSuffix: string | null = null;

  constructor(config: FakeTabConfig) {
    const diagnostics: Diagnostics = {
      warn: (message) => {
        this.warnings.push(message);
      },
      error: (message, cause) => {
        this.errors.push({ message, cause });
      },
    };
    const shared = config.capability === "shared";
    this.transport = {
      openEventSource: (url, init) => {
        if (this.failOnSuffix !== null && url.endsWith(this.failOnSuffix)) {
          this.failOnSuffix = null;
          throw new Error("source construction failed");
        }
        const source = new FakeEventSource(url, init);
        this.sources.push(source);
        return source;
      },
      openChannel: shared
        ? () => {
            const channel = new FakeChannel(config.network.channels);
            this.channels.push(channel);
            return channel;
          }
        : null,
      locks: shared ? config.network.locks : null,
      page: this.page,
      wallNow: config.wallNow,
      newTabId: () => config.network.nextTabId(),
      diagnostics,
      facts: { secureContext: config.secureContext, locksObject: shared },
    };
  }

  failNextSourceOnPath(suffix: string): void {
    this.failOnSuffix = suffix;
  }
}
