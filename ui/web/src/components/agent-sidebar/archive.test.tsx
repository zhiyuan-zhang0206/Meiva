import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentArchive } from "./archive";
import type { InnerProps } from "./types";
import type { AgentRow } from "@/lib/contracts/types";
import { renderWithApi as render } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});


vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
const props = {
  activeId: null,
  onSelect: vi.fn(),
  onTerminate: vi.fn(),
  onRestart: vi.fn(),
  onForceKill: vi.fn(),
  onFork: vi.fn(),
  onRename: vi.fn(),
  pendingActions: {},
  showStatusDot: false,
  showActivity: false,
  confirmTerminate: false,
  confirmRestart: false,
  confirmForceKill: false,
  wide: true,
} as unknown as InnerProps & { wide: boolean };
const row = (id: number): AgentRow => ({
  agent_id: id,
  spawner: "user",
  harness: "meiva",
  fork_source_agent_id: null,
  status: "terminated",
  spawned_at: "2026-06-06T00:00:00Z",
  last_active_at: "2026-06-06T00:00:00Z",
  last_inbound_at: "2026-06-06T00:00:00Z",
  label: null,
  machine: "test",
  supports_vision: true,
  awaiting_response_count: 0,
  highest_notice_priority: null,
  unread_notice_count: 0,
  heartbeat_paused_until: null,
  liveness_state: "offline",
});
let client: QueryClient;
beforeEach(() => { vi.resetAllMocks(); client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); });
afterEach(() => { cleanup(); client.clear(); });
function mount() {
  return render(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={client}><AgentArchive props={props} /></QueryClientProvider>
    </WallClockProvider>,
  );
}

describe("bounded agent archive", () => {
  it("keeps one page, releases the previous page, and opens an ID directly", async () => {
    transport.listAgents.mockImplementation(({ beforeId } = {}) => Promise.resolve(beforeId == null ? { agents: [row(10)], next_cursor: 10 } : { agents: [row(9)], next_cursor: null }));
    mount();
    await waitFor(() => expect(screen.getByText("#10")).toBeTruthy());
    fireEvent.click(screen.getByText("archiveOlder"));
    await waitFor(() => expect(screen.getByText("#9")).toBeTruthy());
    expect(screen.queryByText("#10")).toBeNull();
    await waitFor(() => expect(client.getQueryCache().findAll({ queryKey: ["agent-directory"] })).toHaveLength(1));
    fireEvent.click(screen.getByText("#9"));
    expect(props.onSelect).toHaveBeenCalledWith(9);
    expect(transport.listAgents).toHaveBeenLastCalledWith(expect.objectContaining({ scope: "terminated", beforeId: 10, limit: 50 }));
  });
  it("changing search cancels the old request and cannot display its late result", async () => {
    let signal: AbortSignal | undefined;
    let finish!: (value: { agents: AgentRow[]; next_cursor: null }) => void;
    transport.listAgents.mockImplementation(({ query, signal: requestSignal } = {}) => {
      if (!query) { signal = requestSignal; return new Promise((resolve) => { finish = resolve; }); }
      return Promise.resolve({ agents: [row(22)], next_cursor: null });
    });
    mount();
    await waitFor(() => expect(signal).toBeDefined());
    fireEvent.change(screen.getByLabelText("archiveSearch"), { target: { value: "target" } });
    await waitFor(() => expect(screen.getByText("#22")).toBeTruthy());
    expect(signal?.aborted).toBe(true);
    await act(async () => { finish({ agents: [row(99)], next_cursor: null }); await Promise.resolve(); });
    expect(screen.queryByText("#99")).toBeNull();
  });
});
