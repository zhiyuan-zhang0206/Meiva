// meiva:note
// The streams the tests control: the same context values the app's providers publish, with a
// `subscribe` that records the handlers. A test drives a frame by calling `streamHandlers.system`
// (or `connection`), so the component still receives it the way an SSE frame arrives and no
// EventSource is opened. Nothing replaces the stream module.
import type { ReactNode } from "react";

import type { ConnectionEvent, SystemEvent } from "@/lib/contracts/types";
import { FakeNetwork, FakeTab } from "@/lib/transport/stream/tests/fake-stream";
import { StreamHub } from "@/lib/transport/stream/stream-hub";
import { AgentEventStreamContext, EventStreamContext } from "@/lib/transport/useEventStream";

type SystemHandler = (event: SystemEvent) => void;
type ConnectionHandler = (event: ConnectionEvent) => void;
type BatchHandler = (events: SystemEvent[]) => void;

export const streamHandlers = {
  system: undefined as SystemHandler | undefined,
  connection: undefined as ConnectionHandler | undefined,
  batch: undefined as BatchHandler | undefined,
};

// meiva:note
// Forget the previous test's subscribers and their handlers.
export function resetStreamHandlers(): void {
  streamHandlers.system = undefined;
  streamHandlers.connection = undefined;
  streamHandlers.batch = undefined;
}

function subscribe(
  onSystem: SystemHandler,
  onConnection: ConnectionHandler,
  onBatch?: BatchHandler,
): () => void {
  streamHandlers.system = onSystem;
  streamHandlers.connection = onConnection;
  streamHandlers.batch = onBatch;
  return () => {
    if (streamHandlers.system === onSystem) streamHandlers.system = undefined;
    if (streamHandlers.connection === onConnection) streamHandlers.connection = undefined;
    if (streamHandlers.batch === onBatch) streamHandlers.batch = undefined;
  };
}

// meiva:note
// One value for both contexts: the global and the per-agent stream carry the same shape, and
// an identity that never changes keeps a subscriber from re-subscribing on every render.
const streamValue = { subscribe };

export function TestStreamProvider({ children }: { children?: ReactNode }) {
  return (
    <EventStreamContext.Provider value={streamValue}>
      <AgentEventStreamContext.Provider value={streamValue}>{children}</AgentEventStreamContext.Provider>
    </EventStreamContext.Provider>
  );
}

// meiva:note
// A hub over a fake tab: a page that mounts the real stream providers works without an
// EventSource, and `hub` is what <StreamHubProvider> expects. The hub is the same seam the
// app's composition root fills with the browser transport; `tab` records the sources a page
// constructed, so a test can assert that nothing connected.
export function fakeStreamHub(): { hub: StreamHub; tab: FakeTab } {
  const tab = new FakeTab({
    network: new FakeNetwork(),
    capability: "legacy",
    wallNow: () => Date.now(),
    secureContext: true,
  });
  const hub = new StreamHub(
    tab.transport,
    { checkAuth: () => Promise.resolve({ authenticated: true }), sessionInvalid: () => undefined },
    "",
  );
  return { hub, tab };
}
