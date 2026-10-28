import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { USER_SETTING_DEFAULTS, type ModelsResponse, type SystemStatus } from "@/lib/contracts/types";
import { useWarmHarnessAvailability } from "@/lib/harness/use-harness-availability";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";

import { SpawnForm } from "./spawn-form";

let storedSettings: Record<string, unknown> = {};

function resetMockSettings(initial?: Record<string, unknown>) {
  storedSettings = initial ? { ...initial } : {};
}

function setMockSetting(key: string, value: unknown) {
  storedSettings = { ...storedSettings, [key]: value };
}

vi.mock("@/lib/transport/api", () => ({
  api: {
    getSystemStatus: vi.fn(),
    getModels: vi.fn(),
    getHarnessAvailability: vi.fn().mockResolvedValue([]),
    getSettings: () =>
      Promise.resolve({
        settings: Object.entries(storedSettings).map(([key, value]) => ({
          key,
          value,
          updated_at: "2026-05-19T00:00:00Z",
        })),
      }),
    putSetting: (key: string, value: unknown) => {
      storedSettings = { ...storedSettings, [key]: value };
      return Promise.resolve({ key, value, updated_at: "2026-05-19T00:00:00Z" });
    },
  },
}));

import { api } from "@/lib/transport/api";

function wrap(layout?: "table" | "stack") {
  const onSpawn = vi.fn();
  const onDone = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...storedSettings });
  render(
    <QueryClientProvider client={qc}>
      <SpawnForm onSpawn={onSpawn} onDone={onDone} layout={layout} />
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

const modelsDefault = (): ModelsResponse => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {},
  default: "deepseek-v4-pro",
});

const modelsWithEffort = (): ModelsResponse => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      harnesses: ["meiva"],
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
      pricing: { input: 1.65, cache_read: 0.14, output: 6.6 },
    },
    "claude-opus-4-8": {
      provider: "claude",
      harnesses: ["meiva", "claude_code"],
      context_window: 200_000,
      reasoning_effort_options: null,
      reasoning_effort_default: null,
    },
  },
  default: "deepseek-v4-pro",
});

const modelsWithTwoLadders = (): ModelsResponse => ({
  providers: { deepseek: ["deepseek-v4-pro"], gpt: ["gpt-5.6"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      harnesses: ["meiva"],
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "gpt-5.6": {
      provider: "gpt",
      harnesses: ["meiva", "codex"],
      harness_settings: { codex: { reasoning_effort_options: ["low", "medium", "high"], reasoning_effort_default: "medium" } },
      context_window: 400_000,
      reasoning_effort_options: ["minimal", "low", "medium", "high"],
      reasoning_effort_default: "medium",
    },
  },
  default: "deepseek-v4-pro",
});

const harnessAllAvailable = () =>
  (["meiva", "claude_code", "codex", "antigravity", "kimi_code"] as const).map((harness) => ({
    harness,
    status: "available" as const,
    reason: "",
    launchable: true,
    billing: harness === "meiva" ? ("api" as const) : ("subscription" as const),
  }));

const MACHINE_COLUMN = "Machine";
const HARNESS_COLUMN = "Harness";
const MODEL_COLUMN = "Model";
const EFFORT_COLUMN = "Effort";

async function table(): Promise<HTMLElement> {
  return await screen.findByTestId("spawn-table");
}

function column(element: HTMLElement, name: string): HTMLElement {
  return within(element).getByRole("list", {
    name,
  });
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

describe("spawn table model & effort columns", () => {
  it("a metered harness shows three rates per model, a subscription one shows none", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    wrap();
    let form = await table();
    // meiva:note
    // Rates are the decision only when the harness meters them (user 2026-10-20):
    // input / cache read / output for the built-in runtime, nothing for a
    // subscription harness.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    expect(within(column(form, MODEL_COLUMN)).getByText(/\$[\d.]+ \/ \$[\d.]+ \/ \$[\d.]+/)).toBeTruthy();
    pick(form, HARNESS_COLUMN, "Claude Code");
    form = await table();
    expect(within(column(form, MODEL_COLUMN)).queryByText(/\$[\d.]+ \/ \$[\d.]+ \/ \$[\d.]+/)).toBeNull();
  });

  it("a provider switched off in Control contributes no models", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    setMockSetting("models.hidden_providers", ["claude"]);
    wrap();
    const form = await table();
    expect(within(column(form, MODEL_COLUMN)).queryByText("claude-opus-4-8")).toBeNull();
    expect(within(column(form, MODEL_COLUMN)).getByText("deepseek-v4-pro")).toBeTruthy();
  });

  it("selecting an effort sends it", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "high");
    expect(selected(form, EFFORT_COLUMN, "high")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
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
        "deepseek-v4-pro": { provider: "deepseek", harnesses: ["meiva"], context_window: 128_000, superseded_by: "deepseek-flash" },
        "deepseek-flash": { provider: "deepseek", harnesses: ["meiva"], context_window: 128_000, superseded_by: null },
        "deepseek-legacy": { provider: "deepseek", harnesses: ["meiva"], context_window: 1, superseded_by: null },
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

  it("lists every agent-runner machine, a down host disabled, and spawns on the chosen one", async () => {
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
    // meiva:note
    // The machine is the first column, so several hosts are rows rather than a
    // footer list; the down host keeps its row, disabled and titled with the
    // reason (user 2026-10-20).
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    expect(selected(form, MACHINE_COLUMN, "a-host")).toBe(true);
    expect(row(form, MACHINE_COLUMN, /b-host/).hasAttribute("disabled")).toBe(true);
    pick(form, MACHINE_COLUMN, "a-host");
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
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

// meiva:note
// The narrow arrangement: on a phone the popover is 94vw and the four-column
// table left the model column 4px wide (measured 2026-10-21), so the same four
// decisions render as sections whose closed rows state the current pick. These
// cases pin the properties the table cases pin - the pick reaches the spawn, a
// pick that is never spawned is not stored, a refusal is readable - against the
// layout the phone actually gets.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
describe("stacked picker", () => {
  async function stack(): Promise<HTMLElement> {
    return await screen.findByTestId("spawn-stack");
  }

  function section(form: HTMLElement, name: string): HTMLElement {
    return within(form).getByRole("button", { name });
  }

  it("states the current pick in each closed section", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    wrap("stack");
    const form = await stack();
    expect(section(form, "Machine test-host").getAttribute("aria-expanded")).toBe("false");
    expect(section(form, "Harness Meiva")).toBeTruthy();
    expect(section(form, "Model deepseek-v4-pro")).toBeTruthy();
    expect(section(form, "Effort max")).toBeTruthy();
    expect(within(form).queryByRole("list", { name: MODEL_COLUMN })).toBeNull();
    expect(screen.getByRole("button", { name: "Spawn" })).toBeTruthy();
  });

  it.each([
    { harness: "claude_code", label: "Claude Code", model: "claude-opus-4-8", effort: "—", catalog: modelsWithEffort },
    { harness: "codex", label: "Codex", model: "gpt-5.6", effort: "medium", catalog: modelsWithTwoLadders },
  ] as const)("names $harness execution choices in stacked sections", async ({ harness, label, model, effort, catalog }) => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(catalog());
    setMockSetting("behavior.spawn_harness", harness);
    wrap("stack");
    const form = await stack();
    expect(section(form, `Model ${model}`)).toBeTruthy();
    expect(section(form, `Effort ${effort}`)).toBeTruthy();

    fireEvent.click(section(form, `Model ${model}`));
    expect(within(form).getByRole("list", { name: "Model" })).toBeTruthy();
    expect(selected(form, MODEL_COLUMN, model)).toBe(true);
    fireEvent.click(section(form, `Effort ${effort}`));
    expect(within(form).getByRole("list", { name: "Effort" })).toBeTruthy();

    fireEvent.click(section(form, `Harness ${label}`));
    pick(form, HARNESS_COLUMN, "Meiva");
    expect(section(form, "Model deepseek-v4-pro")).toBeTruthy();
    expect(section(form, "Effort max")).toBeTruthy();
  });

  it("sends the model and effort selected for an external harness on mobile", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    const catalog = modelsWithTwoLadders();
    catalog.models["gpt-5.6"].harness_settings = {
      codex: { reasoning_effort_options: ["low", "medium", "ultra"], reasoning_effort_default: "medium" },
    };
    vi.mocked(api.getModels).mockResolvedValue(catalog);
    setMockSetting("behavior.spawn_harness", "codex");
    const { onSpawn } = wrap("stack");
    const form = await stack();
    expect(section(form, "Model gpt-5.6")).toBeTruthy();
    fireEvent.click(section(form, "Effort medium"));
    pick(form, EFFORT_COLUMN, "ultra");
    expect(section(form, "Effort ultra")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host", harness: "codex", model: "gpt-5.6", reasoning_effort: "ultra",
    });
  });

  it("one pick closes its section and reaches the spawn", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap("stack");
    const form = await stack();
    fireEvent.click(section(form, "Model deepseek-v4-pro"));
    const models = within(form).getByRole("list", { name: MODEL_COLUMN });
    fireEvent.click(within(models).getByRole("button", { name: /claude-opus-4-8/ }));
    expect(section(form, "Model claude-opus-4-8")).toBeTruthy();
    expect(within(form).queryByRole("list", { name: MODEL_COLUMN })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith(
      expect.objectContaining({ machine: "test-host", model: "claude-opus-4-8" }),
    );
  });

  it("a dead host carries its reason as text, not a hover title", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
        { name: "dead-host", online: true, agentHostOnline: false },
      ]),
    );
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    wrap("stack");
    const form = await stack();
    fireEvent.click(section(form, "Machine test-host"));
    const machines = within(form).getByRole("list", { name: MACHINE_COLUMN });
    const dead = within(machines).getByRole("button", { name: /dead-host/ });
    expect(dead.hasAttribute("disabled")).toBe(true);
    expect(within(dead).getByText("Agent host unavailable")).toBeTruthy();
  });

  it("a refusal is written beside the action instead of titled", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    vi.mocked(api.getHarnessAvailability).mockResolvedValue(
      (["meiva", "claude_code", "codex", "antigravity"] as const).map((harness) => ({
        harness,
        status: "unavailable" as const,
        reason: "missing",
        launchable: false,
        billing: "subscription" as const,
      })),
    );
    wrap("stack");
    await stack();
    expect(screen.getByTestId("spawn-refusal").textContent).toBe("unavailable");
    expect(screen.getByRole("button", { name: "Spawn" }).hasAttribute("disabled")).toBe(true);
  });
});

// meiva:note
// One probe per page load (user 2026-10-21): the sidebar warms every agent-runner
// machine when the page mounts, and opening the picker afterwards answers from
// that reading instead of dispatching another probe. The probe is not free — the
// gateway RPCs the machine and the probe stats binaries and login stores — so a
// session must not re-ask on every open.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
describe("harness availability freshness", () => {
  it("a warm page answers the picker without asking again", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue(singleMachineStatus());
    vi.mocked(api.getModels).mockResolvedValue(modelsDefault());
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Warm() {
      useWarmHarnessAvailability();
      return null;
    }
    const warm = render(
      <QueryClientProvider client={qc}>
        <Warm />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(vi.mocked(api.getHarnessAvailability).mock.calls.length).toBeGreaterThan(0));
    const probed = vi.mocked(api.getHarnessAvailability).mock.calls.length;
    warm.unmount();
    render(
      <QueryClientProvider client={qc}>
        <SpawnForm onSpawn={vi.fn()} onDone={vi.fn()} />
      </QueryClientProvider>,
    );
    await screen.findByTestId("spawn-table");
    expect(vi.mocked(api.getHarnessAvailability).mock.calls.length).toBe(probed);
  });
});
