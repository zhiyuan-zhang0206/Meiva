import { QueryClient } from "@tanstack/react-query";
import { cleanup, screen } from "@testing-library/react";
import { renderWithProviders } from "@/test-support/render";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENTS_QUERY_KEY } from "@/lib/agents/use-agents";
import { USER_SETTING_DEFAULTS, type AgentRow } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";

// meiva:note
// vitest+happy-dom has no localStorage by default — the view persists its mobile
// tab there, so provide a simple in-memory mock.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
const localStorageMock = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, writable: true });
Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: vi.fn(),
});

let storedSettings: Record<string, unknown> = {};
const putSettingMock = vi.fn((key: string, value: unknown) => {
  storedSettings = { ...storedSettings, [key]: value };
  return Promise.resolve({ key, value, updated_at: "2026-06-06T00:00:00Z" });
});

import { BAR_HEIGHT_CLASS } from "@/lib/layout/layout";
import { FleetView } from "./fleet-view";

import { resetTransport, transport } from "@/test-support/fake-api";

// meiva:note
// The route hands its surfaces a `navigate`; a suite passes a spy, so a jump can be read
// without replacing `next/navigation`.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
const navigate = vi.fn();
beforeEach(() => {
  resetTransport();
  transport.getSettings.mockImplementation(() =>
      Promise.resolve({
        settings: Object.entries(storedSettings).map(([key, value]) => ({
          key,
          value,
          updated_at: "2026-06-06T00:00:00Z",
        })),
      }));
  transport.putSetting.mockImplementation((key: string, value: unknown) => putSettingMock(key, value));
});

beforeEach(() => {
  storedSettings = {};
  window.innerWidth = 1024;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, "", "/fleet");
  window.innerWidth = 1024;
});

// meiva:note
// The test door for this file: the roster, the empty task registry and the empty
// page list enter through the caches the hooks read — the shape the app stores — and the transport
// answers the reads the panels still make, including the graph the Agents tab mounts. The settings
// cache holds what the test stated, so a persisted choice is a value, not a stubbed store.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
function wrap(ui: ReactElement, { agents = [] }: { agents?: AgentRow[] } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...storedSettings });
  qc.setQueryData(AGENTS_QUERY_KEY, { agents, ancestors: [] });
  for (const window of ["24h", "7d", "30d", "all"] as const) {
    qc.setQueryData(["tasks", "graph", window], { tasks: [] });
  }
  transport.getTasks.mockResolvedValue({ tasks: [] });
  transport.getFleetGraph.mockResolvedValue({
    nodes: [],
    edges: [],
    telemetry_stale: false,
    snapshot_at: null,
  });
  return renderWithProviders(ui, {
    queryClient: qc,
    settings: storedSettings,
    wallNow: () => Date.now(),
  });
}

function makeAgent(over: Partial<AgentRow> & { agent_id: number }): AgentRow {
  return {
    spawner: "user",
    harness: "meiva",
    fork_source_agent_id: null,
    status: "running",
    spawned_at: "2026-06-06T00:00:00Z",
    last_active_at: "2026-06-06T00:00:00Z", last_inbound_at: "2026-06-06T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    llm_model: null, reasoning_effort: null,
    has_pending_question: false, has_unread_user_messages: false,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...over,
  };
}

describe("FleetView (desktop)", () => {
  it("shows active/total counts and the graph surface", () => {
    const agents: AgentRow[] = [
      makeAgent({ agent_id: 1, label: "lead", status: "idling" }),
      makeAgent({ agent_id: 2, spawner: "agent:1", label: "worker" }),
      makeAgent({ agent_id: 3, spawner: "agent:1", status: "terminated" }),
    ];
    wrap(<FleetView navigate={navigate} />, { agents });
    // 2 alive (idling + running), 1 terminated
    expect(screen.getByText("2 active · 3 total")).toBeTruthy();
    expect(screen.getByLabelText("Agent graph legend")).toBeTruthy();
  });

  it("shows 0 active · 0 total when there are no agents", () => {
    const agents: AgentRow[] = [];
    wrap(<FleetView navigate={navigate} />, { agents });
    expect(screen.getByText("0 active · 0 total")).toBeTruthy();
  });

  it("root fills the pane width (flex-1) regardless of content (task #1066)", () => {
    // Regression: main is a row-flex landmark, so a root without flex-1
    // shrinks to its content max-width — clicking a short Inbox item
    // collapsed the whole fleet surface to the left ~60% of the viewport.
    const agents: AgentRow[] = [];
    const { container } = wrap(<FleetView navigate={navigate} />, { agents });
    const root = container.firstElementChild as HTMLElement;
    expect(root.classList.contains("flex-1")).toBe(true);
  });

  it("pins the Agents/Tasks bar to the shared bar height", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1 })];
    wrap(<FleetView navigate={navigate} />, { agents });
    const heightClass = (el: Element) => [...el.classList].find((c) => /^h-\d/.test(c));
    expect(heightClass(screen.getByText("Agents").closest("div")!)).toBe(BAR_HEIGHT_CLASS);
  });

  it("links the header back to the conversation", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1 })];
    wrap(<FleetView navigate={navigate} />, { agents });
    const link = screen.getByLabelText("Back to conversation");
    expect(link.getAttribute("href")).toBe("/");
  });

  it("has no Inbox, no Ask queue and no queue panel handle", () => {
    wrap(<FleetView navigate={navigate} />, { agents: [makeAgent({ agent_id: 1 })] });
    expect(screen.queryByText("Ask")).toBeNull();
    expect(screen.queryByLabelText("Collapse queue")).toBeNull();
    expect(screen.queryByLabelText("Expand queue")).toBeNull();
  });

});

describe("FleetView (mobile)", () => {
  beforeEach(() => {
    window.innerWidth = 375;
  });

  it("renders the Agents / Tasks mobile tabs; Agents shows the graph", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, label: "lead", status: "running" })];
    wrap(<FleetView navigate={navigate} />, { agents });
    const tablist = screen.getByRole("tablist", { name: "Fleet sections" });
    expect(tablist.tagName).toBe("DIV");
    expect(screen.getByRole("tab", { name: /Agents/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Tasks/ })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /Ask/ })).toBeNull();
    expect(screen.getByLabelText("Agent graph legend")).toBeTruthy();
  });

  it("persists mobile tab selection across remounts", () => {
    localStorage.setItem("meiva.fleet.mobileTab", "tasks");
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, label: "lead" })];
    wrap(<FleetView navigate={navigate} />, { agents });
    expect(screen.getByRole("tab", { name: /Tasks/, selected: true })).toBeTruthy();
  });

  it("ignores a stored tab that no longer exists", () => {
    localStorage.setItem("meiva.fleet.mobileTab", "inbox");
    wrap(<FleetView navigate={navigate} />, { agents: [makeAgent({ agent_id: 1 })] });
    expect(screen.getByRole("tab", { name: /Agents/, selected: true })).toBeTruthy();
  });
});
