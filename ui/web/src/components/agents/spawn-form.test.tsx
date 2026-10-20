// meiva:note
// SpawnForm renders inside the sidebar's create popover, so every test mounts it
// directly. Agent processes only run on agent-runner machines: a form with no
// spawnable host says why instead of offering the table, one spawnable host makes
// the footer a single button, several list the hosts there. The form is one table
// (user 2026-10-20): harness, then the models that harness runs, then the efforts
// that model offers — each column narrows the next, and the stored spawn config
// marks its rows selected as soon as it renders.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpawnForm } from "./spawn-form";
import type { SystemStatus } from "@/lib/contracts/types";

vi.mock("@/lib/transport/api", () => ({
  api: {
    getSystemStatus: vi.fn(),
    getModels: vi.fn(),
    getHarnessAvailability: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { api } from "@/lib/transport/api";
import { mockSetSettingCalls, resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

function wrap() {
  const onSpawn = vi.fn();
  const onDone = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <SpawnForm onSpawn={onSpawn} onDone={onDone} />
    </QueryClientProvider>,
  );
  return { onSpawn, onDone };
}

function statusWith(
  machines: {
    name: string;
    online: boolean;
    paused?: boolean | null;
    agentHostOnline?: boolean | null;
  }[],
  current = "cloud",
): SystemStatus {
  return {
    cluster: {
      current_machine: current,
      current_serve_gateway: true,
      current_serve_agent_runner: false,
      current_serve_observability_station: false,
      current_paused: false,
      machines: machines.map((m) => ({
        name: m.name,
        serve_gateway: m.name === current,
        serve_agent_runner: m.name !== current,
        serve_observability_station: false,
        identity_mismatch: false,
        is_staging: false,
        gateway_url: `http://${m.name}:8000`,
        up_since_at: "2026-05-19T00:00:00Z",
        online: m.online,
        paused: m.paused !== undefined ? m.paused : m.online ? false : null,
        agent_host_online: m.agentHostOnline !== undefined ? m.agentHostOnline : m.online ? true : null,
        shell_count: 0,
        agent_count: 0,
        session_count: 0,
        agent_groups: [],
        resource: null,
      })),
    },
    services: { items: [] },
  };
}

const singleMachineStatus = () =>
  statusWith([
    { name: "cloud", online: true },
    { name: "test-host", online: true },
  ]);

const modelsDefault = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {},
  default: "deepseek-v4-pro",
});

const modelsWithEffort = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "claude-opus-4-8": {
      provider: "claude",
      context_window: 200_000,
      reasoning_effort_options: null,
      reasoning_effort_default: null,
    },
  },
  default: "deepseek-v4-pro",
});

const modelsWithTwoLadders = () => ({
  providers: { deepseek: ["deepseek-v4-pro"], gpt: ["gpt-5.6"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "gpt-5.6": {
      provider: "gpt",
      context_window: 400_000,
      reasoning_effort_options: ["minimal", "low", "medium", "high"],
      reasoning_effort_default: "medium",
    },
  },
  default: "deepseek-v4-pro",
});

const harnessAllAvailable = () =>
  (["meiva", "claude_code", "codex", "antigravity"] as const).map((harness) => ({
    harness,
    status: "available" as const,
    reason: "",
    launchable: true,
  }));

const HARNESS_COLUMN = "Harness";
const MODEL_COLUMN = "Model";
const EFFORT_COLUMN = "Effort";

async function table(): Promise<HTMLElement> {
  return await screen.findByTestId("spawn-table");
}

function column(element: HTMLElement, name: string): HTMLElement {
  return within(element).getByRole("list", { name });
}

function row(element: HTMLElement, name: string, option: string | RegExp): HTMLElement {
  return within(column(element, name)).getByRole("button", { name: option });
}

function selected(element: HTMLElement, name: string, option: string | RegExp): boolean {
  return row(element, name, option).getAttribute("aria-pressed") === "true";
}

function pick(element: HTMLElement, name: string, option: string | RegExp) {
  fireEvent.click(row(element, name, option));
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  resetMockSettings();
  vi.mocked(api.getModels).mockResolvedValue({ providers: {}, models: {}, default: "" });
  vi.mocked(api.getHarnessAvailability).mockResolvedValue(harnessAllAvailable());
});

describe("no spawnable agent host", () => {
  it("a reachable runner with a down host says so instead of showing the table", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([{ name: "cloud", online: true }, { name: "test-host", online: true, agentHostOnline: false }]),
    );
    wrap();
    expect(await screen.findByText("Agent host unavailable")).toBeTruthy();
    expect(screen.queryByTestId("spawn-table")).toBeNull();
  });

  it("an absent host verdict reads as unknown", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([{ name: "cloud", online: true }, { name: "test-host", online: true, agentHostOnline: null }]),
    );
    wrap();
    expect(await screen.findByText("Agent host status unknown")).toBeTruthy();
  });

  it("no agent-runner at all", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(statusWith([{ name: "cloud", online: true }]));
    wrap();
    expect(await screen.findByText("No agent runner with an available host")).toBeTruthy();
  });
});

describe("spawn table", () => {
  it("one spawnable runner → the footer spawns there with the resolved config", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const { onSpawn, onDone } = wrap();
    const form = await table();
    expect(selected(form, MODEL_COLUMN, /deepseek-v4-pro/)).toBe(true);
    expect(selected(form, EFFORT_COLUMN, "max")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn on test-host" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "deepseek-v4-pro",
      reasoning_effort: "max",
    });
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("opens with the stored spawn config marked selected", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    setMockSetting("behavior.spawn_harness", "claude_code");
    setMockSetting("behavior.spawn_model", "claude-opus-4-8");
    wrap();
    const form = await table();
    expect(selected(form, HARNESS_COLUMN, "Claude Code")).toBe(true);
    expect(selected(form, MODEL_COLUMN, /claude-opus-4-8/)).toBe(true);
    // meiva:note
    // A model without an effort ladder leaves the column empty.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    expect(within(column(form, EFFORT_COLUMN)).queryByRole("button")).toBeNull();
  });

  it("a pick that is never spawned does not become the stored config", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    setMockSetting("behavior.spawn_model", "deepseek-v4-pro");
    wrap();
    const form = await table();
    pick(form, MODEL_COLUMN, /claude-opus-4-8/);
    expect(selected(form, MODEL_COLUMN, /claude-opus-4-8/)).toBe(true);
    // meiva:note
    // Closing without spawning keeps the previous spawn's config: the next open
    // must answer "what did the last agent run on", not "what did I click last"
    // (user 2026-10-20). A click that wrote through would surface here.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    cleanup();
    wrap();
    const reopened = await table();
    expect(selected(reopened, MODEL_COLUMN, /deepseek-v4-pro/)).toBe(true);
  });

  it("a spawn stores the config it used, cascade included", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "high");
    fireEvent.click(screen.getByRole("button", { name: "Spawn on test-host" }));
    // meiva:note
    // No stored model or effort means the cascade filled them in; the spawn
    // writes what it actually sent, so the next open marks the same rows.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    expect(mockSetSettingCalls()).toEqual([
      { key: "behavior.spawn_model", value: "deepseek-v4-pro" },
      { key: "behavior.spawn_reasoning_effort", value: "high" },
    ]);
  });

  it("changing the harness keeps only the models that harness runs", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap();
    const form = await table();
    expect(row(form, MODEL_COLUMN, /deepseek-v4-pro/)).toBeTruthy();
    pick(form, HARNESS_COLUMN, "Claude Code");
    expect(within(column(form, MODEL_COLUMN)).queryByText("deepseek-v4-pro")).toBeNull();
    expect(selected(form, MODEL_COLUMN, /claude-opus-4-8/)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn on test-host" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "claude-opus-4-8",
      reasoning_effort: undefined,
      harness: "claude_code",
    });
  });

  it("a harness the machine cannot start is disabled in its column", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    vi.mocked(api.getHarnessAvailability).mockResolvedValue([
      { harness: "meiva", status: "available", reason: "", launchable: true },
      { harness: "claude_code", status: "unavailable", reason: "not signed in", launchable: false },
    ]);
    wrap();
    const form = await table();
    await waitFor(() => {
      expect(row(form, HARNESS_COLUMN, "Claude Code").hasAttribute("disabled")).toBe(true);
    });
  });

  it("selecting an effort sends it", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "high");
    expect(selected(form, EFFORT_COLUMN, "high")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn on test-host" }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: "high" }));
  });

  it("switching model drops an effort the new model does not offer", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithTwoLadders());
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "max");
    pick(form, MODEL_COLUMN, /gpt-5.6/);
    const efforts = within(column(form, EFFORT_COLUMN)).getAllByRole("button").map((b) => b.textContent);
    expect(efforts).toEqual(["minimal", "low", "medium", "high"]);
    expect(selected(form, EFFORT_COLUMN, "medium")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn on test-host" }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ model: "gpt-5.6", reasoning_effort: "medium" }));
  });

  it("groups the model column by provider", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    wrap();
    const form = await table();
    const models = column(form, MODEL_COLUMN);
    expect(within(models).getByText("DeepSeek")).toBeTruthy();
    expect(within(models).getByText("Claude")).toBeTruthy();
  });

  it("excludes roster-hidden and superseded models", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue({
      providers: { deepseek: ["deepseek-v4-pro", "deepseek-flash", "deepseek-legacy"] },
      models: {
        "deepseek-v4-pro": { provider: "deepseek", context_window: 128_000, superseded_by: "deepseek-flash" },
        "deepseek-flash": { provider: "deepseek", context_window: 128_000, superseded_by: null },
        "deepseek-legacy": { provider: "deepseek", context_window: 1, superseded_by: null },
      },
      default: "deepseek-flash",
    });
    setMockSetting("models.hidden", ["deepseek-legacy"]);
    wrap();
    const form = await table();
    const models = column(form, MODEL_COLUMN);
    expect(within(models).getByText("deepseek-flash")).toBeTruthy();
    expect(within(models).queryByText("deepseek-v4-pro")).toBeNull();
    expect(within(models).queryByText("deepseek-legacy")).toBeNull();
  });

  it("several spawnable hosts are listed in the footer, a down one disabled", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "a-host", online: true },
        { name: "b-host", online: true, agentHostOnline: false },
      ]),
    );
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    const { onSpawn } = wrap();
    const form = await table();
    expect(within(form).queryByRole("button", { name: "Spawn on a-host" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^a-host/ }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ machine: "a-host" }));
  });
});

describe("unavailable model catalog", () => {
  it("says so instead of rendering the table", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([{ name: "runner-a", online: true, paused: false, agentHostOnline: true }]),
    );
    vi.mocked(api.getModels).mockRejectedValue(new Error("catalog down"));
    wrap();
    expect(await screen.findByText("Models unavailable")).toBeTruthy();
    expect(screen.queryByTestId("spawn-table")).toBeNull();
  });
});
