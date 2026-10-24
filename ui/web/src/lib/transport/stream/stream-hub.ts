import { SSE_INIT, type SseChannel } from "./sse-plan";
import { ProfileSseTransport } from "./sse-share";
import type { EventSourceHandle, SessionPort, StreamTransport } from "../transport";

export function sharedSseSupported(transport: Pick<StreamTransport, "openChannel" | "locks">): boolean {
  return transport.openChannel !== null && transport.locks !== null;
}

export function transportModeReport(transport: StreamTransport): { key: "shared" | "fallback"; value: string } {
  const { secureContext, locksObject } = transport.facts;
  return {
    key: sharedSseSupported(transport) ? "shared" : "fallback",
    value: `secure=${Number(secureContext)},locks=${Number(locksObject)},bc=${Number(transport.openChannel !== null)}`,
  };
}

// meiva:note
// Owns the shared-stream objects of one page: the per-channel-set transports
// (providers that ask for the same channel set share one source), the single
// unmerged transport, and whether the transport-mode event has been reported.
// The composition root builds one hub; tests build their own with a double
// StreamTransport, so no stream state lives at module level.
// meiva:link ui/web/src/docs/frontend-data-flow/merged-stream.meiva.okf.md
export class StreamHub {
  private shared: ProfileSseTransport | null = null;
  private readonly mergedTransports = new Map<string, ProfileSseTransport>();
  private modeReported = false;

  constructor(
    readonly transport: StreamTransport,
    readonly session: SessionPort,
    readonly base: string,
  ) {}

  transportFor(merged: readonly SseChannel[]): ProfileSseTransport | null {
    const supported = sharedSseSupported(this.transport);
    if (merged.length === 0) return supported ? this.sharedTransport() : null;
    const key = merged.join(",");
    let transport = this.mergedTransports.get(key);
    if (transport === undefined) {
      transport = new ProfileSseTransport(this.transport, this.session, {
        base: this.base,
        merged: new Set(merged),
        solo: !supported,
      });
      this.mergedTransports.set(key, transport);
    }
    return transport;
  }

  openLegacy(path: string): EventSourceHandle {
    return this.transport.openEventSource(`${this.base}${path}`, SSE_INIT);
  }

  modeEventOnce(): { key: "shared" | "fallback"; value: string } | null {
    if (this.modeReported) return null;
    this.modeReported = true;
    return transportModeReport(this.transport);
  }

  dispose(): void {
    this.shared?.dispose();
    this.shared = null;
    for (const transport of this.mergedTransports.values()) transport.dispose();
    this.mergedTransports.clear();
  }

  private sharedTransport(): ProfileSseTransport {
    this.shared ??= new ProfileSseTransport(this.transport, this.session, {
      base: this.base,
      merged: new Set(),
      solo: false,
    });
    return this.shared;
  }
}
