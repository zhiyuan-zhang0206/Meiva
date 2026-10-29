import { modelLabel, reasoningEffortLabel } from "@/lib/contracts/models";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { USER_SETTING_DEFAULTS, type ModelsResponse, type SystemStatus } from "@/lib/contracts/types";
import type { HarnessAvailability } from "@/lib/harness/harness";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";

import { SpawnForm } from "./spawn-form";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getHarnessAvailability.mockResolvedValue([]);
  transport.getSettings.mockImplementation(() =>
      Promise.resolve({
        settings: Object.entries(storedSettings).map(([key, value]) => ({
          key,
          value,
          updated_at: "2026-05-19T00:00:00Z",
        })),
      }));
  transport.putSetting.mockImplementation((key: string, value: unknown) => {
      storedSettings = { ...storedSettings, [key]: value };
      return Promise.resolve({ key, value, updated_at: "2026-05-19T00:00:00Z" });
    });
});


let storedSettings: Record<string, unknown> = {};

function resetMockSettings(initial?: Record<string, unknown>) {
  storedSettings = initial ? { ...initial } : {};
}

function setMockSetting(key: string, value: unknown) {
  storedSettings = { ...storedSettings, [key]: value };
}


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
  const raw = typeof option === "string" ? option : option.source;
  const label = name === MODEL_COLUMN ? modelLabel(raw) : name === EFFORT_COLUMN ? reasoningEffortLabel(raw) : raw;
  return within(column(element, name)).getByRole("button", { name: typeof option === "string" ? label : new RegExp(label, option.flags) });
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
  transport.getModels.mockResolvedValue({ providers: {}, models: {}, default: "" });
  transport.getHarnessAvailability.mockResolvedValue(harnessAllAvailable());
});

describe("spawn table model & effort columns", () => {
  it("a metered harness shows three rates per model, a subscription one shows none", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    wrap();
    let form = await table();
    // meiva:note
    // Rates are the decision only when the harness meters them (user 2026-10-20):
    // input / cache read / output for the built-in runtime, nothing for a
    // subscription harness.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    expect(within(column(form, MODEL_COLUMN)).getByText(/\$[\d.]+ \/ \$[\d.]+ \/ \$[\d.]+/)).toBeTruthy();
    pick(form, HARNESS_COLUMN, "Claude Code");
    form = await table();
    expect(within(column(form, MODEL_COLUMN)).queryByText(/\$[\d.]+ \/ \$[\d.]+ \/ \$[\d.]+/)).toBeNull();
  });

  it("a provider switched off in Control contributes no models", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    setMockSetting("models.hidden_providers", ["claude"]);
    wrap();
    const form = await table();
    expect(within(column(form, MODEL_COLUMN)).queryByText(modelLabel("claude-opus-4-8"))).toBeNull();
    expect(within(column(form, MODEL_COLUMN)).getByText(modelLabel("deepseek-v4-pro"))).toBeTruthy();
  });

  it("selecting an effort sends it", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "high");
    expect(selected(form, EFFORT_COLUMN, "high")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: "high" }));
  });

  it("switching model drops an effort the new model does not offer", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithTwoLadders());
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "max");
    pick(form, MODEL_COLUMN, /gpt-5.6/);
    const efforts = within(column(form, EFFORT_COLUMN)).getAllByRole("button").map((b) => b.textContent);
    expect(efforts).toEqual(["Minimal", "Low", "Medium", "High"]);
    expect(selected(form, EFFORT_COLUMN, "medium")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ model: "gpt-5.6", reasoning_effort: "medium" }));
  });

  it("groups the model column by provider", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsDefault());
    wrap();
    const form = await table();
    const models = column(form, MODEL_COLUMN);
    expect(within(models).getByText("DeepSeek")).toBeTruthy();
    expect(within(models).getByText("Claude")).toBeTruthy();
  });

  it("excludes roster-hidden and superseded models", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue({
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
    expect(within(models).getByText(modelLabel("deepseek-flash"))).toBeTruthy();
    expect(within(models).queryByText(modelLabel("deepseek-v4-pro"))).toBeNull();
    expect(within(models).queryByText(modelLabel("deepseek-legacy"))).toBeNull();
  });

  it("lists every agent-runner machine, a down host disabled, and spawns on the chosen one", async () => {
    transport.getSystemStatus.mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "a-host", online: true },
        { name: "b-host", online: true, agentHostOnline: false },
      ]),
    );
    transport.getModels.mockResolvedValue(modelsDefault());
    const { onSpawn } = wrap();
    const form = await table();
    // meiva:note
    // The machine is the first column, so several hosts are rows rather than a
    // footer list; the down host keeps its row, disabled and titled with the
    // reason (user 2026-10-20).
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    expect(selected(form, MACHINE_COLUMN, "a-host")).toBe(true);
    expect(row(form, MACHINE_COLUMN, /b-host/).hasAttribute("disabled")).toBe(true);
    pick(form, MACHINE_COLUMN, "a-host");
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ machine: "a-host" }));
  });
});

describe("unavailable model catalog", () => {
  it("keeps refresh available while preventing a spawn without the catalog", async () => {
    transport.getSystemStatus.mockResolvedValue(
      statusWith([{ name: "runner-a", online: true, paused: false, agentHostOnline: true }]),
    );
    transport.getModels.mockRejectedValue(new Error("catalog down"));
    wrap();
    expect(await screen.findByText("Models unavailable")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Spawn" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  });
});

// meiva:note
// The narrow arrangement: on a phone the popover is 94vw and the four-column
// table left the model column 4px wide (measured 2026-10-21), so the same four
// decisions render as sections whose closed rows state the current pick. These
// cases pin the properties the table cases pin - the pick reaches the spawn, a
// pick that is never spawned is not stored, a refusal is readable - against the
// layout the phone actually gets.
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
describe.each(["table", "stack"] as const)("%s API model visibility", (layout) => {
  it.each([
    { harness: "claude_code", label: "Claude Code", provider: "claude", model: "claude-sonnet-5-5" },
    { harness: "codex", label: "Codex", provider: "gpt", model: "gpt-6-astra" },
    { harness: "antigravity", label: "Antigravity", provider: "gemini", model: "gemini-4-argon" },
    { harness: "kimi_code", label: "Kimi Code", provider: "kimi", model: "kimi-k2.6" },
  ] as const)("keeps $harness execution selectable while its API model is hidden", async ({ harness, label, provider, model }) => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    const catalog = modelsWithEffort();
    catalog.providers[provider] = [model];
    catalog.models[model] = {
      provider,
      harnesses: ["meiva", harness],
      context_window: 128_000,
      harness_settings: {
        [harness]: { reasoning_effort_options: ["low", "medium"], reasoning_effort_default: "medium" },
      },
    };
    transport.getModels.mockResolvedValue(catalog);
    setMockSetting("behavior.spawn_harness", harness);
    setMockSetting("models.hidden", [model]);
    setMockSetting("models.hidden_providers", [provider]);
    const { onSpawn } = wrap(layout);
    const form = await screen.findByTestId(`spawn-${layout}`);
    if (layout === "stack") {
      expect(within(form).getByRole("button", { name: `Model ${modelLabel(model)}` })).toBeTruthy();
      fireEvent.click(within(form).getByRole("button", { name: "Effort Medium" }));
    } else {
      expect(selected(form, MODEL_COLUMN, model)).toBe(true);
    }
    pick(form, EFFORT_COLUMN, "low");
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host", harness, model, reasoning_effort: "low",
    });
    expect(storedSettings["models.hidden"]).toEqual([model]);
    expect(storedSettings["models.hidden_providers"]).toEqual([provider]);

    if (layout === "stack") {
      fireEvent.click(within(form).getByRole("button", { name: `Harness ${label}` }));
    }
    pick(form, HARNESS_COLUMN, "Meiva");
    if (layout === "stack") {
      fireEvent.click(within(form).getByRole("button", { name: "Model DeepSeek V4 Pro" }));
    }
    expect(within(column(form, MODEL_COLUMN)).queryByRole("button", { name: modelLabel(model) })).toBeNull();
    expect(selected(form, MODEL_COLUMN, /deepseek-v4-pro/)).toBe(true);
  });
});

describe("stacked picker", () => {
  async function stack(): Promise<HTMLElement> {
    return await screen.findByTestId("spawn-stack");
  }

  function section(form: HTMLElement, name: string): HTMLElement {
    return within(form).getByRole("button", { name });
  }

  it("states the current pick in each closed section", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    wrap("stack");
    const form = await stack();
    expect(section(form, "Machine test-host").getAttribute("aria-expanded")).toBe("false");
    expect(section(form, "Harness Meiva")).toBeTruthy();
    expect(section(form, "Model DeepSeek V4 Pro")).toBeTruthy();
    expect(section(form, "Effort Max")).toBeTruthy();
    expect(within(form).queryByRole("list", { name: MODEL_COLUMN })).toBeNull();
    expect(screen.getByRole("button", { name: "Spawn" })).toBeTruthy();
  });

  it.each([
    { harness: "claude_code", label: "Claude Code", model: "claude-opus-4-8", effort: "—", catalog: modelsWithEffort },
    { harness: "codex", label: "Codex", model: "gpt-5.6", effort: "medium", catalog: modelsWithTwoLadders },
  ] as const)("names $harness execution choices in stacked sections", async ({ harness, label, model, effort, catalog }) => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(catalog());
    setMockSetting("behavior.spawn_harness", harness);
    wrap("stack");
    const form = await stack();
    expect(section(form, `Model ${modelLabel(model)}`)).toBeTruthy();
    expect(section(form, `Effort ${reasoningEffortLabel(effort)}`)).toBeTruthy();

    fireEvent.click(section(form, `Model ${modelLabel(model)}`));
    expect(within(form).getByRole("list", { name: "Model" })).toBeTruthy();
    expect(selected(form, MODEL_COLUMN, model)).toBe(true);
    fireEvent.click(section(form, `Effort ${reasoningEffortLabel(effort)}`));
    expect(within(form).getByRole("list", { name: "Effort" })).toBeTruthy();

    fireEvent.click(section(form, `Harness ${label}`));
    pick(form, HARNESS_COLUMN, "Meiva");
    expect(section(form, "Model DeepSeek V4 Pro")).toBeTruthy();
    expect(section(form, "Effort Max")).toBeTruthy();
  });

  it("sends the model and effort selected for an external harness on mobile", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    const catalog = modelsWithTwoLadders();
    catalog.models["gpt-5.6"].harness_settings = {
      codex: { reasoning_effort_options: ["low", "medium", "ultra"], reasoning_effort_default: "medium" },
    };
    transport.getModels.mockResolvedValue(catalog);
    setMockSetting("behavior.spawn_harness", "codex");
    const { onSpawn } = wrap("stack");
    const form = await stack();
    expect(section(form, "Model GPT 5.6")).toBeTruthy();
    fireEvent.click(section(form, "Effort Medium"));
    pick(form, EFFORT_COLUMN, "ultra");
    expect(section(form, "Effort Ultra")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host", harness: "codex", model: "gpt-5.6", reasoning_effort: "ultra",
    });
  });

  it("one pick closes its section and reaches the spawn", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap("stack");
    const form = await stack();
    fireEvent.click(section(form, "Model DeepSeek V4 Pro"));
    const models = within(form).getByRole("list", { name: MODEL_COLUMN });
    fireEvent.click(within(models).getByRole("button", { name: /Claude Opus 4.8/ }));
    expect(section(form, "Model Claude Opus 4.8")).toBeTruthy();
    expect(within(form).queryByRole("list", { name: MODEL_COLUMN })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith(
      expect.objectContaining({ machine: "test-host", model: "claude-opus-4-8" }),
    );
  });

  it("a dead host carries its reason as text, not a hover title", async () => {
    transport.getSystemStatus.mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "test-host", online: true },
        { name: "dead-host", online: true, agentHostOnline: false },
      ]),
    );
    transport.getModels.mockResolvedValue(modelsDefault());
    wrap("stack");
    const form = await stack();
    fireEvent.click(section(form, "Machine test-host"));
    const machines = within(form).getByRole("list", { name: MACHINE_COLUMN });
    const dead = within(machines).getByRole("button", { name: /dead-host/ });
    expect(dead.hasAttribute("disabled")).toBe(true);
    expect(within(dead).getByText("Agent host unavailable")).toBeTruthy();
  });

  it("a refusal is written beside the action instead of titled", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsDefault());
    transport.getHarnessAvailability.mockResolvedValue(
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

describe.each(["table", "stack"] as const)("manual harness refresh (%s)", (layout) => {
  beforeEach(() => {
    resetMockSettings();
    transport.getHarnessAvailability.mockResolvedValue([]);
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithTwoLadders());
  });

  it("opening and reopening only reads snapshots; an explicit click checks the selected machine", async () => {
    transport.refreshHarnessAvailability.mockResolvedValue([
      { harness: "meiva", status: "available", reason: "", launchable: true, billing: "api" },
      { harness: "codex", status: "available", reason: "", launchable: true, billing: "subscription" },
    ]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = () => render(<QueryClientProvider client={qc}><SpawnForm onSpawn={vi.fn()} onDone={vi.fn()} layout={layout} /></QueryClientProvider>);
    const first = view();
    await screen.findByTestId(`spawn-${layout}`);
    await waitFor(() => expect(transport.getHarnessAvailability).toHaveBeenCalled());
    expect(transport.refreshHarnessAvailability).not.toHaveBeenCalled();
    first.unmount();
    view();
    await screen.findByTestId(`spawn-${layout}`);
    expect(transport.refreshHarnessAvailability).not.toHaveBeenCalled();
    const refresh = screen.getByRole("button", { name: "Refresh" });
    const spawn = screen.getByRole("button", { name: "Spawn" });
    expect(refresh.compareDocumentPosition(spawn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(refresh);
    await waitFor(() => expect(transport.refreshHarnessAvailability).toHaveBeenCalledExactlyOnceWith("test-host"));
    await waitFor(() => expect(qc.getQueryData(["harness-availability", "test-host"])).toMatchObject([
      { harness: "meiva" }, { harness: "codex", launchable: true },
    ]));
    expect(transport.putSetting).not.toHaveBeenCalled();
  });

  it("keeps previous choices and shows a failed refresh without retrying automatically", async () => {
    transport.getHarnessAvailability.mockResolvedValue([
      { harness: "codex", status: "available", reason: "", launchable: true, billing: "subscription" },
    ]);
    resetMockSettings({ "behavior.spawn_harness": "codex", "behavior.spawn_model": "gpt-5.6", "behavior.spawn_reasoning_effort": "high" });
    transport.refreshHarnessAvailability.mockRejectedValue(new Error("offline"));
    const { onSpawn } = wrap(layout);
    await screen.findByTestId(`spawn-${layout}`);
    await waitFor(() => expect(transport.getHarnessAvailability).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByRole("alert");
    expect(transport.refreshHarnessAvailability).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({ machine: "test-host", harness: "codex", model: "gpt-5.6", reasoning_effort: "high" });
  });

  it("shows a shared pending state across picker reopen and rejects repeated clicks", async () => {
    const pending = Promise.withResolvers<HarnessAvailability[]>();
    transport.refreshHarnessAvailability.mockReturnValue(pending.promise);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = () => render(<QueryClientProvider client={qc}><SpawnForm onSpawn={vi.fn()} onDone={vi.fn()} layout={layout} /></QueryClientProvider>);
    const first = view();
    await screen.findByTestId(`spawn-${layout}`);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const busy = await screen.findByRole("button", { name: "Refreshing…" });
    expect(busy.hasAttribute("disabled")).toBe(true);
    fireEvent.click(busy);
    first.unmount();
    view();
    expect((await screen.findByRole("button", { name: "Refreshing…" })).hasAttribute("disabled")).toBe(true);
    expect(transport.refreshHarnessAvailability).toHaveBeenCalledTimes(1);
    pending.resolve(harnessAllAvailable());
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" }).hasAttribute("disabled")).toBe(false));
  });

  it("keeps a late refresh result on its original machine after switching targets", async () => {
    transport.getSystemStatus.mockResolvedValue(statusWith([
      { name: "cloud", online: true }, { name: "a-host", online: true }, { name: "b-host", online: true },
    ]));
    const pending = Promise.withResolvers<HarnessAvailability[]>();
    transport.refreshHarnessAvailability.mockReturnValue(pending.promise);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><SpawnForm onSpawn={vi.fn()} onDone={vi.fn()} layout={layout} /></QueryClientProvider>);
    const form = await screen.findByTestId(`spawn-${layout}`);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    if (layout === "stack") fireEvent.click(within(form).getByRole("button", { name: "Machine a-host" }));
    pick(form, MACHINE_COLUMN, "b-host");
    pending.resolve(harnessAllAvailable());
    await waitFor(() => expect(qc.getQueryData(["harness-availability", "a-host"])).toEqual(harnessAllAvailable()));
    expect(qc.getQueryData(["harness-availability", "b-host"])).toEqual([]);
    expect(transport.refreshHarnessAvailability).toHaveBeenCalledExactlyOnceWith("a-host");
  });
});
