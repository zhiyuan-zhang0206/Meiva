"use client";

// One visible document holds the profile lock and owns all three gateway SSE
// sockets. Other visible documents receive raw frames and connection state over
// BroadcastChannel. The fallback lives in the existing Providers.

import { API_BASE, api } from "./api";
import { notifySessionInvalid } from "../auth/auth-context";
import { track } from "../telemetry/telemetry";

export type SseChannel = "system" | "alerts" | "systemAll";
export type SseSlot = SseChannel | "merged";
export type SseState = "open" | "reconnecting" | "closed";
export interface SseListener {
  onFrame: (raw: string) => void;
  onState: (state: SseState) => void;
  onGap?: (dropped: number) => void;
}

const WIRE_CHANNEL: Record<SseChannel, string> = {
  system: "global",
  systemAll: "all",
  alerts: "alerts",
};
const LOGICAL_CHANNEL: Record<string, SseChannel | undefined> = {
  global: "system",
  all: "systemAll",
  alerts: "alerts",
};
const MERGED_ORDER: readonly SseChannel[] = ["system", "systemAll", "alerts"];
const ALL_SLOTS: readonly SseSlot[] = ["system", "alerts", "systemAll", "merged"];

type Message =
  | { type: "frame"; ch: SseChannel; data: string; leaderId: string }
  | { type: "gap"; ch: SseChannel; n: number; leaderId: string }
  | { type: "state"; ch: SseChannel; state: SseState; leaderId: string }
  | { type: "interest"; tabId: string; activeId: number | null }
  | { type: "leave"; tabId: string }
  | { type: "ping"; leaderId: string }
  | { type: "restart"; ch: SseChannel; backoff: boolean }
  | { type: "sessionInvalid"; leaderId: string };

const NAME = "meiva-ui-sse";
// A ping requests a fresh interest announcement every 15s. A closed tab is
// removed after 45-50s (three missed announcements plus a 5s sweep);
// explicit leave is faster.
const PING_MS = 15_000;
const INTEREST_TTL_MS = 45_000;
const PRUNE_MS = 5_000;
const UNION_DEBOUNCE_MS = 250;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

interface Environment {
  channel: BroadcastChannel | null;
  locks: LockManager | null;
  document: Document;
  source: (url: string) => EventSource;
  checkAuth: typeof api.checkAuth;
  sessionInvalid: () => void;
  now: () => number;
}

interface SourceSlot {
  source: EventSource | null;
  url: string | null;
  retry: ReturnType<typeof setTimeout> | null;
  failures: number;
  lastRestartAt: number;
}

function newSlot(): SourceSlot {
  return { source: null, url: null, retry: null, failures: 0, lastRestartAt: -Infinity };
}

export function sharedSseSupported(): boolean {
  if (typeof BroadcastChannel === "undefined" || typeof navigator === "undefined") return false;
  const locks: unknown = Reflect.get(navigator, "locks");
  return typeof locks === "object" && locks !== null &&
    "request" in locks && typeof locks.request === "function";
}

let modeReported = false;

/** One mode event per tab session, after an authenticated stream starts. */
export function reportSseTransportMode(): void {
  if (modeReported) return;
  const locks: unknown = typeof navigator === "undefined" ? undefined : Reflect.get(navigator, "locks");
  const hasLocks = typeof locks === "object" && locks !== null;
  const hasBC = typeof BroadcastChannel !== "undefined";
  const context: unknown = typeof window === "undefined" ? false : Reflect.get(window, "isSecureContext");
  const secure = context === true;
  track("sse-transport", {
    key: sharedSseSupported() ? "shared" : "fallback",
    value: `secure=${Number(secure)},locks=${Number(hasLocks)},bc=${Number(hasBC)}`,
  });
  modeReported = true;
}

export class ProfileSseTransport {
  // Only needs to distinguish concurrent documents; it is not an auth token.
  private readonly tabId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  private readonly listeners: Record<SseChannel, Set<SseListener>> = {
    system: new Set(), alerts: new Set(), systemAll: new Set(),
  };
  private readonly detailInterests = new Map<SseListener, number | null>();
  private readonly states: Record<SseChannel, SseState | null> = {
    system: null, alerts: null, systemAll: null,
  };
  private readonly slots: Record<SseSlot, SourceSlot> = {
    system: newSlot(), alerts: newSlot(), systemAll: newSlot(), merged: newSlot(),
  };
  private readonly interests = new Map<string, { activeId: number | null; seen: number }>();
  private participating = false;
  private leader = false;
  private currentLeader: string | null = null;
  private pendingLock: AbortController | null = null;
  private releaseLock: (() => void) | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private pruneTimer: ReturnType<typeof setInterval> | null = null;
  private unionTimer: ReturnType<typeof setTimeout> | null = null;
  private detailUnion = "";
  private disposed = false;

  // meiva:note
  // `merged` is the set of logical channels one `/api/stream` source carries;
  // empty means the per-channel endpoints, which is the switch-off behaviour.
  // `solo` owns that source without the profile lock (plain HTTP has no
  // navigator.locks, and the per-provider fallback is exactly the case that
  // exhausts a browser's six connections per origin).
  // meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
  constructor(
    private readonly env: Environment,
    private readonly merged: ReadonlySet<SseChannel> = new Set(),
    private readonly solo = false,
  ) {
    if (env.channel) {
      env.channel.onmessage = (event: MessageEvent<unknown>) => this.receive(event.data);
    }
    env.document.addEventListener("visibilitychange", this.syncParticipation);
  }

  subscribe(ch: SseChannel, listener: SseListener, activeId: number | null = null): () => void {
    this.listeners[ch].add(listener);
    if (ch === "systemAll") this.detailInterests.set(listener, activeId);
    if (this.participating) this.announceInterest();
    this.syncParticipation();
    const state = this.states[ch];
    if (state !== null && this.participating) {
      queueMicrotask(() => {
        if (this.listeners[ch].has(listener) && this.participating && this.states[ch] === state)
          listener.onState(state);
      });
    }
    return () => {
      this.listeners[ch].delete(listener);
      if (ch === "systemAll") this.detailInterests.delete(listener);
      if (this.participating) this.announceInterest();
      this.syncParticipation();
    };
  }

  isLeader(): boolean { return this.leader; }

  /** A follower can request repair; only the lock holder touches EventSource. */
  restart(ch: SseChannel, backoff = false): void {
    if (!this.participating) return;
    if (this.leader) this.restartOwned(this.slotOf(ch), backoff);
    else this.post({ type: "restart", ch, backoff });
  }

  /** Also useful for document teardown; a browser releases the lock on close. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.env.document.removeEventListener("visibilitychange", this.syncParticipation);
    this.stopParticipation();
    queueMicrotask(() => this.env.channel?.close());
  }

  private readonly syncParticipation = (): void => {
    const hasListeners = Object.values(this.listeners).some((set) => set.size > 0);
    const shouldParticipate = !this.disposed && hasListeners &&
      this.env.document.visibilityState === "visible";
    if (shouldParticipate === this.participating) return;
    if (!shouldParticipate) {
      this.stopParticipation();
      return;
    }
    this.participating = true;
    this.announceInterest();
    this.requestLock();
  };

  private stopParticipation(): void {
    if (!this.participating) return;
    this.participating = false;
    this.post({ type: "leave", tabId: this.tabId });
    this.pendingLock?.abort();
    this.releaseLock?.();
    if (this.solo && this.leader) {
      this.stopLeader();
      this.leader = false;
    }
    this.currentLeader = null;
  }

  private requestLock(): void {
    if (this.solo) {
      this.becomeSoloLeader();
      return;
    }
    if (this.pendingLock || !this.participating) return;
    const controller = new AbortController();
    this.pendingLock = controller;
    const locks = this.env.locks;
    if (locks === null) throw new Error("shared SSE lock manager missing");
    void locks.request(NAME, { mode: "exclusive", signal: controller.signal }, async () => {
      if (!this.participating) return;
      this.leader = true;
      this.currentLeader = this.tabId;
      try {
        this.startLeader();
        await new Promise<void>((resolve) => { this.releaseLock = resolve; });
      } finally {
        this.releaseLock = null;
        try {
          this.stopLeader();
        } finally {
          this.leader = false;
          this.currentLeader = null;
        }
      }
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") return;
      this.stopParticipation();
      console.error("[sse-share] Web Lock failed", error);
    }).finally(() => {
      this.pendingLock = null;
      if (this.participating) this.requestLock();
    });
  }

  private becomeSoloLeader(): void {
    if (this.leader || !this.participating) return;
    this.leader = true;
    this.currentLeader = this.tabId;
    try {
      this.startLeader();
    } catch (error) {
      this.stopLeader();
      this.leader = false;
      this.currentLeader = null;
      console.error("[sse-share] merged source failed", error);
    }
  }

  private post(message: Message): void { this.env.channel?.postMessage(message); }

  private slotOf(ch: SseChannel): SseSlot { return this.merged.has(ch) ? "merged" : ch; }

  private carriedChannels(slot: SseSlot): readonly SseChannel[] {
    if (slot !== "merged") return [slot];
    return MERGED_ORDER.filter((ch) => this.merged.has(ch));
  }

  private localActiveId(): number | null {
    for (const id of this.detailInterests.values()) if (id !== null) return id;
    return null;
  }

  private announceInterest(): void {
    const activeId = this.localActiveId();
    if (this.leader) {
      this.interests.set(this.tabId, { activeId, seen: this.env.now() });
      this.scheduleUnion();
    } else {
      this.post({ type: "interest", tabId: this.tabId, activeId });
    }
  }

  private receive(raw: unknown): void {
    if (!raw || typeof raw !== "object" || !("type" in raw)) return;
    const message = raw as Message;
    switch (message.type) {
      case "interest": {
        if (!this.leader) return;
        const firstAnnouncement = !this.interests.has(message.tabId);
        this.interests.set(message.tabId, { activeId: message.activeId, seen: this.env.now() });
        this.scheduleUnion();
        // A newly visible follower may have missed the original open message.
        if (firstAnnouncement) {
          this.post({ type: "ping", leaderId: this.tabId });
          for (const ch of ["system", "alerts", "systemAll"] as const) {
            const state = this.states[ch];
            if (state !== null) this.post({ type: "state", ch, state, leaderId: this.tabId });
          }
        }
        return;
      }
      case "leave":
        if (!this.leader) return;
        this.interests.delete(message.tabId);
        this.scheduleUnion();
        return;
      case "ping":
        if (!this.participating || this.leader) return;
        this.currentLeader = message.leaderId;
        this.announceInterest();
        return;
      case "frame":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.deliverFrame(message.ch, message.data);
        return;
      case "gap":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.deliverGap(message.ch, message.n);
        return;
      case "state":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.deliverState(message.ch, message.state);
        return;
      case "restart":
        if (this.leader) this.restartOwned(this.slotOf(message.ch), message.backoff);
        return;
      case "sessionInvalid":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.env.sessionInvalid();
        return;
      default:
        throw new Error("unknown SSE share message");
    }
  }

  private startLeader(): void {
    this.interests.clear();
    this.announceInterest();
    this.post({ type: "ping", leaderId: this.tabId });
    // A promoted leader may still hold the previous leader's cached open state.
    this.publishState("systemAll", this.localActiveId() === null ? "closed" : "reconnecting");
    this.pingTimer = setInterval(() => {
      this.post({ type: "ping", leaderId: this.tabId });
      this.announceInterest();
    }, PING_MS);
    this.pruneTimer = setInterval(() => {
      const cutoff = this.env.now() - INTEREST_TTL_MS;
      for (const [tabId, interest] of this.interests) {
        if (tabId !== this.tabId && interest.seen <= cutoff) this.interests.delete(tabId);
      }
      this.scheduleUnion();
    }, PRUNE_MS);
    if (this.merged.size === 0) {
      this.openSource("system", `${API_BASE}/api/system`);
      this.openSource("alerts", `${API_BASE}/api/alerts/stream`);
      return;
    }
    this.closeSource("system");
    this.closeSource("alerts");
    this.openSource("merged", this.mergedUrl(this.detailUnion));
  }

  private mergedUrl(union: string): string {
    const channels = MERGED_ORDER.filter((ch) => this.merged.has(ch))
      .map((ch) => WIRE_CHANNEL[ch])
      .join(",");
    const agents = this.merged.has("systemAll") ? `&agents=${union === "" ? "0" : union}` : "";
    return `${API_BASE}/api/stream?channels=${channels}${agents}`;
  }

  private stopLeader(): void {
    if (this.pingTimer !== null) clearInterval(this.pingTimer);
    if (this.pruneTimer !== null) clearInterval(this.pruneTimer);
    if (this.unionTimer !== null) clearTimeout(this.unionTimer);
    this.pingTimer = null;
    this.pruneTimer = null;
    this.unionTimer = null;
    for (const slot of ALL_SLOTS) {
      this.closeSource(slot);
      for (const ch of this.carriedChannels(slot)) this.publishState(ch, "closed");
    }
    this.detailUnion = "";
    this.interests.clear();
  }

  private scheduleUnion(): void {
    if (!this.leader) return;
    if (this.unionTimer !== null) clearTimeout(this.unionTimer);
    this.unionTimer = setTimeout(() => {
      this.unionTimer = null;
      const ids = [...new Set([...this.interests.values()]
        .map(({ activeId }) => activeId).filter((id): id is number => id !== null))]
        .sort((a, b) => a - b);
      const union = ids.join(",");
      if (union === this.detailUnion) return;
      this.detailUnion = union;
      this.applyUnion(union);
    }, UNION_DEBOUNCE_MS);
  }

  private applyUnion(union: string): void {
    if (this.merged.has("systemAll")) {
      this.openSource("merged", this.mergedUrl(union));
      return;
    }
    if (union) this.openSource("systemAll", `${API_BASE}/api/system/all?agents=${union}`);
    else {
      this.closeSource("systemAll");
      this.publishState("systemAll", "closed");
    }
  }

  private closeSource(slotName: SseSlot): void {
    const slot = this.slots[slotName];
    if (slot.retry !== null) clearTimeout(slot.retry);
    slot.retry = null;
    slot.source?.close();
    slot.source = null;
    slot.url = null;
  }

  private openSource(slotName: SseSlot, url: string): void {
    this.closeSource(slotName);
    this.publishSlotState(slotName, "reconnecting");
    const slot = this.slots[slotName];
    slot.url = url;
    const source = this.env.source(url);
    slot.source = source;
    source.onopen = () => {
      if (!this.leader || slot.source !== source) return;
      slot.failures = 0;
      this.publishSlotState(slotName, "open");
    };
    source.onmessage = (event) => {
      if (!this.leader || slot.source !== source) return;
      const raw = typeof event.data === "string" ? event.data : "[non-string SSE payload]";
      if (slotName === "merged") this.publishMergedFrame(raw);
      else this.publishFrame(slotName, raw);
    };
    source.onerror = () => {
      if (!this.leader || slot.source !== source) return;
      switch (source.readyState) {
        case EventSource.CLOSED:
          this.publishSlotState(slotName, "closed");
          void this.env.checkAuth().then((result) => {
            if (!this.leader || slot.source !== source) return;
            if (!result.authenticated) {
              this.post({ type: "sessionInvalid", leaderId: this.tabId });
              this.env.sessionInvalid();
            } else this.scheduleRetry(slotName);
          }).catch(() => {
            if (this.leader && slot.source === source) this.scheduleRetry(slotName);
          });
          return;
        case EventSource.CONNECTING:
          this.publishSlotState(slotName, "reconnecting");
          return;
        case EventSource.OPEN:
          return;
        default:
          throw new Error(`unknown EventSource readyState: ${source.readyState}`);
      }
    };
  }

  private scheduleRetry(slotName: SseSlot): void {
    const slot = this.slots[slotName];
    if (slot.retry !== null || slot.url === null) return;
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** slot.failures, RECONNECT_MAX_MS);
    slot.failures += 1;
    slot.retry = setTimeout(() => {
      slot.retry = null;
      if (this.leader && slot.url !== null) this.openSource(slotName, slot.url);
    }, delay);
  }

  private restartOwned(slotName: SseSlot, backoff: boolean): void {
    const slot = this.slots[slotName];
    if (slot.url === null || slot.retry !== null) return;
    // A watchdog or cluster-update bump can arrive from every visible tab.
    if (this.env.now() - slot.lastRestartAt < RECONNECT_BASE_MS) return;
    slot.lastRestartAt = this.env.now();
    if (backoff) {
      this.publishSlotState(slotName, "reconnecting");
      slot.source?.close();
      this.scheduleRetry(slotName);
    } else this.openSource(slotName, slot.url);
  }

  // meiva:note
  // The merged wire frame names its channel; subscribers keep the per-channel
  // payload shapes they already parse (an object for `system` and `alerts`, the
  // batch array for `systemAll`), so the fan-out lives here and nowhere else.
  // A channel-scoped `error` frame only degrades that channel, and a `dropped`
  // count means the server shed payloads: both surface to providers so they
  // reconcile instead of silently rendering a gap. An unattributable frame is
  // counted by every merged channel, as a corrupt frame is counted today.
  // meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
  private publishMergedFrame(raw: string): void {
    const frame = parseMergedFrame(raw);
    if (frame === null) {
      for (const ch of this.carriedChannels("merged")) this.publishFrame(ch, raw);
      return;
    }
    if (frame.error !== undefined) {
      console.warn(`[sse-share] channel ${frame.ch} degraded: ${frame.error}`);
      this.publishState(frame.ch, "reconnecting");
      return;
    }
    if (frame.events.length > 0) {
      if (frame.ch === "systemAll") {
        this.publishFrame("systemAll", JSON.stringify(frame.events));
      } else {
        for (const event of frame.events) this.publishFrame(frame.ch, JSON.stringify(event));
      }
    }
    if (frame.dropped > 0) this.publishGap(frame.ch, frame.dropped);
  }

  private publishSlotState(slotName: SseSlot, state: SseState): void {
    for (const ch of this.carriedChannels(slotName)) this.publishState(ch, state);
  }

  private deliverFrame(ch: SseChannel, data: string): void {
    for (const listener of this.listeners[ch]) listener.onFrame(data);
  }

  private publishFrame(ch: SseChannel, data: string): void {
    this.deliverFrame(ch, data);
    this.post({ type: "frame", ch, data, leaderId: this.tabId });
  }

  private deliverGap(ch: SseChannel, n: number): void {
    for (const listener of this.listeners[ch]) listener.onGap?.(n);
  }

  private publishGap(ch: SseChannel, n: number): void {
    this.deliverGap(ch, n);
    this.post({ type: "gap", ch, n, leaderId: this.tabId });
  }

  private deliverState(ch: SseChannel, state: SseState): void {
    this.states[ch] = state;
    for (const listener of this.listeners[ch]) listener.onState(state);
  }

  private publishState(ch: SseChannel, state: SseState): void {
    this.deliverState(ch, state);
    this.post({ type: "state", ch, state, leaderId: this.tabId });
  }
}

let shared: ProfileSseTransport | null = null;

export function sharedSseTransport(): ProfileSseTransport {
  if (!sharedSseSupported()) throw new Error("shared SSE APIs unavailable");
  shared ??= new ProfileSseTransport({
    channel: new BroadcastChannel(NAME),
    locks: navigator.locks,
    document,
    source: (url) => new EventSource(url, { withCredentials: true }),
    checkAuth: () => api.checkAuth(),
    sessionInvalid: notifySessionInvalid,
    now: Date.now,
  });
  return shared;
}

interface MergedFrame {
  ch: SseChannel;
  events: unknown[];
  dropped: number;
  error?: string;
}

function parseMergedFrame(raw: string): MergedFrame | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const frame = parsed as { ch?: unknown; events?: unknown; dropped?: unknown; error?: unknown };
  if (typeof frame.ch !== "string") return null;
  const ch = LOGICAL_CHANNEL[frame.ch];
  if (ch === undefined) return null;
  if (typeof frame.error === "string") return { ch, events: [], dropped: 0, error: frame.error };
  if (!Array.isArray(frame.events)) return null;
  const dropped = typeof frame.dropped === "number" && frame.dropped > 0 ? frame.dropped : 0;
  return { ch, events: frame.events, dropped };
}

const mergedTransports = new Map<string, ProfileSseTransport>();

// meiva:note
// Without navigator.locks (plain-HTTP LAN, the setup that exhausts six
// connections) the per-provider fallback is where the sockets multiply, so the
// merged stream runs there too — one page-local source, no lock and no relay.
// An empty set keeps every caller on the single-channel endpoints.
// meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
export function sseTransport(merged: readonly SseChannel[]): ProfileSseTransport | null {
  if (merged.length === 0) return sharedSseSupported() ? sharedSseTransport() : null;
  const key = merged.join(",");
  let transport = mergedTransports.get(key);
  if (transport === undefined) {
    const solo = !sharedSseSupported();
    transport = new ProfileSseTransport(
      {
        channel: solo ? null : new BroadcastChannel(NAME),
        locks: solo ? null : navigator.locks,
        document,
        source: (url) => new EventSource(url, { withCredentials: true }),
        checkAuth: () => api.checkAuth(),
        sessionInvalid: notifySessionInvalid,
        now: Date.now,
      },
      new Set(merged),
      solo,
    );
    mergedTransports.set(key, transport);
  }
  return transport;
}
