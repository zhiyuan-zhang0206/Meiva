

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";


import { DefaultModelPanel } from "./_default_model";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});


function wrap() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DefaultModelPanel id="config-default-model" />
    </QueryClientProvider>,
  );
}

const MODELS = {
  providers: {
    deepseek: ["deepseek-v4-pro", "deepseek-flash"],
    claude: ["claude-sonnet-5"],
  },
  models: {},
  default: "deepseek-v4-pro",
};

beforeEach(() => {
  vi.clearAllMocks();
  transport.getModels.mockResolvedValue(MODELS);
  transport.getDefaultModel.mockResolvedValue({
    model: "deepseek-flash",
    source: "cluster",
  });
  transport.putDefaultModel.mockResolvedValue({
    model: "claude-sonnet-5",
    source: "cluster",
  });
});

afterEach(cleanup);

describe("DefaultModelPanel", () => {
  it("refreshes the cached model picker default after saving", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    qc.setQueryData(["models"], MODELS);
    render(
      <QueryClientProvider client={qc}>
        <DefaultModelPanel id="config-default-model" />
      </QueryClientProvider>,
    );
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-flash"));
    transport.getModels.mockResolvedValue({ ...MODELS, default: "claude-sonnet-5" });
    transport.getDefaultModel.mockResolvedValue({ model: "claude-sonnet-5", source: "cluster" });
    fireEvent.change(select, { target: { value: "claude-sonnet-5" } });
    fireEvent.click(screen.getByTestId("save-default-model"));
    await waitFor(() => expect(qc.getQueryData<{ default: string }>(["models"])?.default).toBe("claude-sonnet-5"));
  });

  it("shows a disabled loading placeholder before the current model resolves", () => {
    transport.getModels.mockReturnValue(new Promise(() => undefined));
    transport.getDefaultModel.mockReturnValue(new Promise(() => undefined));
    wrap();

    const select = screen.getByTestId<HTMLSelectElement>("select-default-model");
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toBe("Loading…");
    expect(select.options[0].disabled).toBe(true);
  });

  it("selects the stored default and groups options by provider", async () => {
    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-flash"));

    const groups = [...select.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groups).toEqual(["DeepSeek", "Claude"]);
    expect([...select.options].map((o) => o.value)).toEqual([
      "deepseek-v4-pro",
      "deepseek-flash",
      "claude-sonnet-5",
    ]);
  });

  it("omits superseded roster options while preserving a stored superseded default", async () => {
    transport.getModels.mockResolvedValue({
      providers: {
        deepseek: ["deepseek-v4-pro", "deepseek-flash", "deepseek-v4-legacy"],
      },
      models: {
        "deepseek-v4-pro": {
          provider: "deepseek",
          context_window: 128_000,
          harnesses: ["meiva"],
          superseded_by: "deepseek-flash",
        },
        "deepseek-flash": {
          provider: "deepseek",
          context_window: 128_000,
          harnesses: ["meiva"],
          superseded_by: null,
        },
        "deepseek-v4-legacy": {
          provider: "deepseek",
          context_window: 128_000,
          harnesses: ["meiva"],
          superseded_by: "deepseek-flash",
        },
      },
      default: "deepseek-flash",
    });
    transport.getDefaultModel.mockResolvedValue({
      model: "deepseek-v4-pro",
      source: "cluster",
    });

    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-v4-pro"));

    expect([...select.options].map((option) => option.value)).toEqual([
      "deepseek-v4-pro",
      "deepseek-flash",
    ]);
  });

  it("PUTs the picked model on save", async () => {
    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-flash"));

    fireEvent.change(select, { target: { value: "claude-sonnet-5" } });
    fireEvent.click(screen.getByTestId("save-default-model"));

    await waitFor(() => expect(transport.putDefaultModel).toHaveBeenCalledWith("claude-sonnet-5"));
  });

  it("keeps save disabled until the pick differs from the stored value", async () => {
    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-flash"));
    const save = screen.getByTestId<HTMLButtonElement>("save-default-model");
    expect(save.disabled).toBe(true);

    fireEvent.change(select, { target: { value: "deepseek-flash" } });
    expect(screen.getByTestId<HTMLButtonElement>("save-default-model").disabled).toBe(true);

    fireEvent.change(select, { target: { value: "deepseek-v4-pro" } });
    expect(screen.getByTestId<HTMLButtonElement>("save-default-model").disabled).toBe(false);
  });

  it("flags a value that is only the .env / code default showing through", async () => {
    transport.getDefaultModel.mockResolvedValue({
      model: "deepseek-v4-pro",
      source: "config",
    });
    wrap();
    expect(await screen.findByTestId("default-model-source")).toBeTruthy();
  });

  it("does not flag a value the cluster chose", async () => {
    wrap();
    await screen.findByTestId("select-default-model");
    await waitFor(() => expect(screen.queryByTestId("default-model-source")).toBeNull());
  });

  it("shows a quiet error instead of the raw failure", async () => {
    transport.getDefaultModel.mockRejectedValue(new Error("boom 500"));
    wrap();
    expect(await screen.findByText(/Couldn't load the default model/)).toBeTruthy();
    expect(screen.queryByText(/boom 500/)).toBeNull();
  });
});

it("keeps the default model read-only when the catalog fails", async () => {
  transport.getModels.mockRejectedValue(new Error("provider private diagnostic"));
  wrap();
  expect(await screen.findByText(/Couldn't load the default model/)).toBeTruthy();
  expect(screen.queryByTestId("save-default-model")).toBeNull();
  expect(screen.queryByText(/provider private diagnostic/)).toBeNull();
});
