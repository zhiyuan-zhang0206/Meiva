import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import {  } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConfigView, ConfigWriteResult } from "@/lib/contracts/types";
import {
  AGENT_RUNNER,
  GATEWAY,
  makeQc,
  OK_RESULT,
  RESOLVED,
  setupConfigTest,
  VIEW,
  wrap,
} from "./config-test-mock";
import ConfigPage from "./page";
import { transport } from "@/test-support/fake-api";

afterEach(cleanup);
beforeEach(setupConfigTest);

describe("ConfigPage text inline edit", () => {
  it("click edit enters edit mode → input has current value", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    expect((input as HTMLInputElement).value).toBe("INFO");
  });

  it("Enter commits → putConfig uses new value", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig
      .mockResolvedValue(OK_RESULT(["log_level"]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    fireEvent.change(input, { target: { value: "DEBUG" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "DEBUG" },
        undefined,
      ),
    );
  });

  it("Escape cancels → putConfig not called", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig
      .mockResolvedValue(OK_RESULT([]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    fireEvent.change(input, { target: { value: "DEBUG" } });
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByTestId("input-log_level")).toBeNull(),
    );
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("unchanged value commit → cancels, putConfig not called", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig
      .mockResolvedValue(OK_RESULT([]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("edit-log_level"));
    fireEvent.click(screen.getByTestId("save-log_level"));
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("int field edit → putConfig receives parseInt-converted number", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig
      .mockResolvedValue(OK_RESULT(["max_retries"]));
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-max_retries"));
    fireEvent.click(screen.getByTestId("edit-max_retries"));
    const input = screen.getByTestId("input-max_retries");
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("save-max_retries"));
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "INFO", max_retries: 5 },
        undefined,
      ),
    );
    const callArg = putSpy.mock.calls[0]?.[0];
    expect(typeof callArg.max_retries).toBe("number");
  });
});

describe("ConfigPage restart banner from write result", () => {
  it("no banner before any write; appears after write with restart targets", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    transport.putConfig.mockResolvedValue(
      OK_RESULT(["log_level"], ["all"]),
    );
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("edit-log_level"));
    expect(
      screen.queryByText(/restart the following processes/),
    ).toBeNull();
    fireEvent.click(screen.getByTestId("edit-log_level"));
    const input = screen.getByTestId("input-log_level");
    fireEvent.change(input, { target: { value: "DEBUG" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const banner = await waitFor(() =>
      screen.getByText(/restart the following processes/i),
    );
    expect(banner.textContent).toMatch(/All processes/);
  });
});

describe("ConfigPage machine selector", () => {
  it("selector renders Cluster + every online machine, not offline", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toEqual(["", GATEWAY, AGENT_RUNNER]);
    expect(select.options[0].textContent).toMatch(/Cluster/);
    expect(select.options[0].textContent).toMatch(new RegExp(GATEWAY));
  });

  it("selecting agent-runner refetches getConfig with machine name", async () => {
    const getSpy = transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    await waitFor(() => expect(getSpy).toHaveBeenCalledWith(undefined));
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() =>
      expect(getSpy).toHaveBeenCalledWith(AGENT_RUNNER),
    );
  });

  it("agent-runner view: host remote_writable field editable, cluster field read-only", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByTestId("edit-host_label"));
    expect(screen.queryByTestId("edit-log_level")).toBeNull();
  });

  it("agent-runner view: can_enable=false bool is disabled with reason", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    const toggle = await waitFor(
      () => screen.getByTestId<HTMLButtonElement>("toggle-browser_enabled"),
    );
    expect(toggle.disabled).toBe(true);
    expect(
      screen.getByTestId("capability-browser_enabled").textContent,
    ).toMatch(/no display detected/);
  });

  it("remote view shows edit-on-Cluster hint for non-host fields", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByText("HOST LABEL"));
    expect(
      screen.getAllByText(/edit on Cluster view/i).length,
    ).toBeGreaterThan(0);
  });

  it("PUT result with !ok field shows reason and reverts value", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig.mockResolvedValue({
      applied: false,
      results: {
        host_label: { ok: false, reason: "value rejected by host" },
      },
      restart_required: [],
    });
    wrap(<ConfigPage />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByTestId("edit-host_label"));
    fireEvent.click(screen.getByTestId("edit-host_label"));
    const input = screen.getByTestId("input-host_label");
    fireEvent.change(input, { target: { value: "rack-Z" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "INFO", host_label: "rack-Z" },
        AGENT_RUNNER,
      ),
    );
    await waitFor(() => screen.getByTestId("error-host_label"));
    expect(
      screen.getByTestId("error-host_label").textContent,
    ).toMatch(/value rejected by host/);
    await waitFor(() => screen.getByText("rack-A"));
    expect(screen.queryByText("rack-Z")).toBeNull();
  });
});

describe("Fix 1 — onSettled invalidates written machine's query key", () => {
  it("toggling cluster then switching machines invalidates cluster key only", async () => {
    let resolvePut!: (v: ConfigWriteResult) => void;
    const putInflight = new Promise<ConfigWriteResult>((res) => {
      resolvePut = res;
    });
    transport.getConfig.mockResolvedValue(VIEW);
    transport.putConfig.mockReturnValue(putInflight);

    const qc = makeQc();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    wrap(<ConfigPage />, qc);

    const toggle = await waitFor(() =>
      screen.getByTestId("toggle-enable_compact"),
    );
    fireEvent.click(toggle);

    const select = screen.getByLabelText<HTMLSelectElement>("Machine");
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });

    resolvePut({
      applied: true,
      results: { enable_compact: { ok: true, reason: null } },
      restart_required: [],
    });

    await waitFor(() => {
      const invalidatedKeys = invalidateSpy.mock.calls.map(
        ([opts]) => opts?.queryKey,
      );
      const hasCluster = invalidatedKeys.some(
        (k) =>
          Array.isArray(k) && k[0] === "config" && k[1] === null,
      );
      expect(hasCluster).toBe(true);
    });

    const invalidatedKeys = invalidateSpy.mock.calls.map(
      ([opts]) => opts?.queryKey,
    );
    const hasWorker = invalidatedKeys.some(
      (k) =>
        Array.isArray(k) &&
        k[0] === "config" &&
        k[1] === AGENT_RUNNER,
    );
    expect(hasWorker).toBe(false);
  });
});

describe("Fix 2 — capability reason hidden on non-editable rows", () => {
  it("cluster-scope bool with can_enable=false on agent-runner: reason NOT shown", async () => {
    const clusterBoolGated: ConfigView = {
      fields: [
        {
          name: "cluster_flag",
          field_type: "bool",
          current_value: false,
          default_value: false,
          description: "A cluster-pinned bool",
          group: "core",
          capability: "common",
          scope: "cluster-pinned",
          restart_required: "",
          writable: false,
          sensitive: false,
          env_var: "MEIVA_CLUSTER_FLAG",
          remote_writable: false,
          per_agent: false,
          can_enable: false,
          reason: "should not appear",
        },
      ],
      raw_overrides: {},
      machine_capabilities: ["agent-runner"],
    };
    transport.getConfig.mockResolvedValue(clusterBoolGated);

    wrap(<ConfigPage />);

    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByText("CLUSTER FLAG"));

    expect(screen.queryByTestId("capability-cluster_flag")).toBeNull();
    expect(screen.queryByText("should not appear")).toBeNull();
  });
});

describe("ConfigPage per-model resolution view", () => {
  it("labels each row with the layer that produced its value", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-source-auto_compact_fraction"));

    expect(screen.getByTestId("per-model-source-auto_compact_fraction").textContent).toBe(
      "model default",
    );
    expect(screen.getByTestId("per-model-source-max_retries").textContent).toBe(".env");
    expect(screen.getByTestId("per-model-source-llm_retry_max_attempts").textContent).toBe(
      "shared default",
    );
    expect(screen.getByTestId("per-model-value-auto_compact_fraction").textContent).toBe(
      "0.55",
    );
    expect(screen.getByTestId("per-model-source-auto_compact_fraction").className).toContain(
      "border-dashed",
    );
    expect(screen.getByTestId("per-model-source-auto_compact_fraction").className).toContain(
      "text-muted-foreground/80",
    );
  });

  it("shows a disabled loading placeholder before models resolve", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    transport.getModels.mockReturnValue(new Promise(() => undefined));
    wrap(<ConfigPage />);

    await screen.findByText("LOG LEVEL");
    const select = screen.getByLabelText<HTMLSelectElement>("Model");
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toBe("Loading…");
    expect(select.options[0].disabled).toBe(true);
  });

  it("shows the layers a value shadowed, and nothing when the floor itself won", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-shadowed-max_retries"));

    expect(screen.getByTestId("per-model-shadowed-max_retries").textContent).toBe(
      "shadows model default 4, shared default 6",
    );

    expect(screen.getByTestId("per-model-shadowed-auto_compact_fraction").textContent).toBe(
      "shadows shared default 0.8",
    );

    expect(screen.queryByTestId("per-model-shadowed-llm_retry_max_attempts")).toBeNull();
  });

  it("re-resolves when another model is picked", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-source-max_retries"));

    const select = screen.getByLabelText<HTMLSelectElement>("Model");
    expect(select.value).toBe("deepseek-v4-pro");

    fireEvent.change(select, { target: { value: "claude-opus-5" } });
    await waitFor(() =>
      expect(transport.getResolvedConfig).toHaveBeenCalledWith("claude-opus-5"),
    );
  });

  it("row link reveals its editor even when the filter had it hidden", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-goto-max_retries"));

    fireEvent.click(screen.getByTestId("filter-sensitive"));
    await waitFor(() =>
      expect(document.getElementById("config-field-max_retries")).toBeNull(),
    );

    fireEvent.click(screen.getByTestId("per-model-goto-max_retries"));

    const row = await waitFor(() => {
      const el = document.getElementById("config-field-max_retries");
      expect(el).not.toBeNull();
      return el;
    });
    expect(row?.className).toContain("ring-primary");

    expect(document.getElementById("config-field-log_level")?.className).not.toContain(
      "ring-primary",
    );
  });

  it("row link expands a persisted-collapsed group before revealing its editor", async () => {
    localStorage.setItem(
      "control.config.collapsedGroups",
      JSON.stringify(["config-llm"]),
    );
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigPage />);
    await screen.findByTestId("per-model-goto-max_retries");
    await waitFor(() =>
      expect(document.getElementById("config-field-max_retries")).toBeNull(),
    );

    fireEvent.click(screen.getByTestId("per-model-goto-max_retries"));

    await waitFor(() =>
      expect(document.getElementById("config-field-max_retries")).not.toBeNull(),
    );
    expect(JSON.parse(localStorage.getItem("control.config.collapsedGroups")!)).not.toContain(
      "config-llm",
    );
  });

  it("flags a model that is not in the registry", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    transport.getResolvedConfig.mockResolvedValue({
      ...RESOLVED,
      model: "some-unknown-model",
      registered: false,
    });
    wrap(<ConfigPage />);
    await waitFor(() => screen.getByTestId("per-model-unregistered"));
  });
});

describe("ConfigPage without a model catalog", () => {
  it("reports model failure while preserving ordinary configuration editing", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    transport.getModels.mockRejectedValue(new Error("provider private diagnostic"));
    transport.getDefaultModel.mockRejectedValue(new Error("model unavailable"));
    const put = transport.putConfig.mockResolvedValue(OK_RESULT(["log_level"]));
    wrap(<ConfigPage />);
    await screen.findByText("Model settings are unavailable. Check the gateway configuration and reload.");
    expect(screen.getByLabelText<HTMLSelectElement>("Model").disabled).toBe(true);
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.queryByText(/provider private diagnostic/)).toBeNull();
    expect(transport.getResolvedConfig).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByTestId("edit-log_level"));
    fireEvent.change(screen.getByTestId("input-log_level"), { target: { value: "DEBUG" } });
    fireEvent.keyDown(screen.getByTestId("input-log_level"), { key: "Enter" });
    await waitFor(() => expect(put).toHaveBeenCalledWith({ log_level: "DEBUG" }, undefined));
  });
});
