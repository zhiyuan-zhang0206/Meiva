import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ConfigView } from "@/lib/contracts/types";
import {
  AGENT_RUNNER,
  inGroups,
  OK_RESULT,
  renderSettled,
  setupConfigTest,
  VIEW,
  wrap,
} from "./config-test-mock";
import ConfigEditor from "./editor";
import { transport } from "@/test-support/fake-api";

afterEach(cleanup);
beforeEach(setupConfigTest);

describe("ConfigPage tri-state", () => {
  it("loading shows loading text", () => {
    transport.getConfig.mockReturnValue(
      new Promise(() => undefined),
    );
    wrap(<ConfigEditor mode="advanced" />);
    expect(screen.getByText(/Loading config/)).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    transport.getConfig.mockRejectedValue(new Error("network down"));
    wrap(<ConfigEditor mode="advanced" />);
    await waitFor(() => screen.getByText(/Couldn't load config/));
    expect(screen.queryByText(/network down/)).toBeNull();
  });
});

describe("ConfigPage render", () => {
  it("collapses groups on demand and restores the per-device state", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    await renderSettled();

    const heading = screen.getByRole("heading", { name: /LLM settings/ });
    const toggle = within(heading).getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(document.getElementById("config-field-log_level")).toBeNull();
    expect(JSON.parse(localStorage.getItem("control.config.collapsedGroups")!)).toContain(
      "config-llm",
    );

    cleanup();
    wrap(<ConfigEditor mode="advanced" />);
    const restoredHeading = await screen.findByRole("heading", { name: /LLM settings/ });
    await waitFor(() =>
      expect(within(restoredHeading).getByRole("button").getAttribute("aria-expanded")).toBe(
        "false",
      ),
    );
    expect(document.getElementById("config-field-log_level")).toBeNull();
  });

  it("ignores malformed collapsed-group storage", async () => {
    localStorage.setItem("control.config.collapsedGroups", "not-json");
    transport.getConfig.mockResolvedValue(VIEW);

    await renderSettled();
    expect(document.getElementById("config-field-log_level")).not.toBeNull();
  });

  it("display-group headers render backend field labels", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    await renderSettled();

    expect(screen.getByText("LLM settings")).toBeTruthy();
    expect(screen.getByText("Agent execution")).toBeTruthy();
    expect(screen.queryByText("Display & general")).toBeNull();
    expect(screen.queryByText("Connection")).toBeNull();

    expect(screen.getByText("Log level")).toBeTruthy();

    expect(inGroups("Max retries")).toHaveLength(1);
    expect(screen.queryByText("log_level")).toBeNull();
    expect(screen.queryByText("MEIVA_LOG_LEVEL")).toBeNull();
  });

  it("HIDDEN_ENV_VARS: transport micro-tuning is dropped from the panel", async () => {

    const viewWithHiddenField: ConfigView = {
      ...VIEW,
      fields: [
        ...VIEW.fields,
        {
          name: "gateway_max_retries",
          label: "Gateway max retries",
          field_type: "int",
          current_value: 3,
          default_value: 3,
          description: "SDK to gateway transport retry count.",
          group: "Gateway",
          capability: "agent-runner",
          scope: "agent",
          restart_required: "",
          writable: false,
          sensitive: false,
          env_var: "MEIVA_GATEWAY_MAX_RETRIES",
          remote_writable: false,
          per_agent: false,
          can_enable: null,
          reason: null,
        },
      ],
    };
    transport.getConfig.mockResolvedValue(viewWithHiddenField);
    await renderSettled();
    expect(screen.queryByText("Gateway max retries")).toBeNull();
    expect(screen.queryByTitle("MEIVA_GATEWAY_MAX_RETRIES")).toBeNull();
  });

  it("field appears under its display-group header", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    await renderSettled();

    const groupHeader = screen.getByText("LLM settings");
    const fieldName = screen.getByText("Log level");
    expect(
      groupHeader.compareDocumentPosition(fieldName) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("uses authored help without repeating technical metadata", async () => {
    transport.getConfig.mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) => field.name === "max_retries"
        ? { ...field, help_text: "Set 0 to disable retries." } : field),
    });
    await renderSettled();

    expect(screen.getByText("Set 0 to disable retries.")).toBeTruthy();
    expect(inGroups("cluster-default")).toHaveLength(0);
    expect(inGroups("per-agent")).toHaveLength(0);
    expect(screen.queryByText("Technical details")).toBeNull();
    expect(screen.queryByText("Logging verbosity")).toBeNull();
    expect(screen.queryByText("Requires restart")).toBeNull();
  });

  it("sensitive field masked + lock; writable secret is write-only replaceable", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig.mockResolvedValue(OK_RESULT(["api_key"]));
    await renderSettled();

    expect(screen.getByText("••••••••")).toBeTruthy();
    expect(screen.queryByText("secret-xyz")).toBeNull();
    expect(screen.getByTestId("sensitive-api_key")).toBeTruthy();

    fireEvent.click(screen.getByTestId("edit-api_key"));
    const input = screen.getByTestId<HTMLInputElement>("input-api_key");
    expect(input.getAttribute("type")).toBe("password");
    expect(screen.getByLabelText("Replace Api key")).toBe(input);
    expect(input.value).toBe("");

    fireEvent.change(input, { target: { value: "sk-NEW" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith({ log_level: "INFO", api_key: "sk-NEW" }, undefined),
    );
  });

  it("sensitive field: an empty submit keeps the secret (no PUT)", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig.mockResolvedValue(OK_RESULT([]));
    wrap(<ConfigEditor mode="advanced" />);
    await waitFor(() => screen.getByTestId("edit-api_key"));
    fireEvent.click(screen.getByTestId("edit-api_key"));
    screen.getByTestId("input-api_key");
    fireEvent.click(screen.getByTestId("save-api_key"));
    expect(putSpy).not.toHaveBeenCalled();
  });

  it("read-only field explains editability without technical badges", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="machines" />);
    await screen.findByText("Build sha");
    expect(screen.queryByTestId("edit-build_sha")).toBeNull();
    expect(within(screen.getByText("Build sha").closest("[id^='config-field-']")!).getByText(/read-only/i)).toBeTruthy();
  });

  it("distinguishes editable values from runtime fields read-only on this machine", async () => {
    transport.getConfig.mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) =>
        field.name === "host_label" ? { ...field, remote_writable: false } : field,
      ),
    });
    wrap(<ConfigEditor mode="machines" />);

    const editableValue = await screen.findByTitle("rack-A");
    expect(editableValue.className).toContain("hover:bg-muted/50");
    expect(editableValue.className).toContain("cursor-text");

    fireEvent.change(screen.getByLabelText("Machine"), {
      target: { value: AGENT_RUNNER },
    });
    const hostLabel = await screen.findByText("Host label");
    await waitFor(() => expect(screen.queryByTestId("edit-host_label")).toBeNull());
    const hostRow = hostLabel.closest<HTMLElement>("[id^='config-field-']")!;
    expect(within(hostRow).queryByText("Runtime")).toBeNull();
    expect(within(hostRow).getByText(/read-only/i)).toBeTruthy();
  });

  it("hides non-actionable fields matched by env var (cluster secret)", async () => {
    const withHidden = {
      ...VIEW,
      fields: [
        ...VIEW.fields,
        {
          name: "cluster_secret",
          label: "Cluster secret",
          field_type: "string" as const,
          current_value: "••••••••",
          default_value: "",
          description: "Cluster pre-shared secret",
          group: "Data plane",
          capability: "gateway" as const,
          scope: "cluster-pinned" as const,
          restart_required: "all" as const,
          writable: true,
          sensitive: true,
          env_var: "MEIVA_CLUSTER_SECRET",
          remote_writable: false,
          per_agent: false,
          can_enable: null,
          reason: null,
        },
      ],
    };
    transport.getConfig.mockResolvedValue(withHidden);
    await renderSettled();

    expect(screen.queryByText("Cluster secret")).toBeNull();
  });

  it("bool field renders a Switch reflecting the current value", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="advanced" />);
    await waitFor(() => screen.getByTestId("toggle-enable_compact"));
    expect(
      screen.getByTestId("toggle-enable_compact").getAttribute("aria-checked"),
    ).toBe("true");
  });
});

describe("ConfigPage tag filter", () => {
  it("filtering by Sensitive hides non-matching rows and empty groups", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    await renderSettled();
    fireEvent.click(screen.getByTestId("filter-sensitive"));

    expect(screen.getByText("Api key")).toBeTruthy();
    expect(screen.queryByText("Log level")).toBeNull();
    expect(screen.queryByText("LLM settings")).toBeNull();
    expect(screen.getByText("1 setting")).toBeTruthy();

    fireEvent.click(screen.getByTestId("filter-all"));
    await waitFor(() => screen.getByText("Log level"));
  });

  it("filtering by CLI-only keeps only non-editable rows", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="machines" />);
    await screen.findByText("Build sha");
    fireEvent.click(screen.getByTestId("filter-cli-only"));

    await waitFor(() => screen.getByText("Build sha"));
    expect(screen.getByText("Gateway port")).toBeTruthy();
    expect(screen.queryByText("Api key")).toBeNull();
    expect(screen.queryByText("Log level")).toBeNull();
  });
});

describe("ConfigPage capability visibility", () => {
  it("Cluster view shows every capability's fields", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="machines" />);
    await screen.findByText("Build sha");

    expect(
      screen.getByTestId("config-group-config-connection").textContent,
    ).toMatch(/Gateway port/);
  });

  it("remote agent-runner view drops gateway-capability fields, keeps the rest", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="machines" />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });

    await waitFor(() => screen.getByText("Host label"));
    expect(screen.queryByText("Gateway port")).toBeNull();
    expect(screen.queryByTestId("config-group-config-connection")).toBeNull();

    expect(screen.queryByText("Log level")).toBeNull();
  });

  it("remote co-located gateway,agent-runner box keeps gateway fields", async () => {

    transport.getConfig.mockResolvedValue({
      ...VIEW,
      machine_capabilities: ["gateway", "agent-runner"],
    });
    wrap(<ConfigEditor mode="machines" />);
    const select = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(select, { target: { value: AGENT_RUNNER } });
    await waitFor(() => screen.getByText("Host label"));
    expect(screen.getByText("Gateway port")).toBeTruthy();
  });
});

describe("ConfigPage bool toggle (putConfig + write result)", () => {
  it("click toggle → putConfig with raw_overrides delta", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig
      .mockResolvedValue(OK_RESULT(["enable_compact"]));
    wrap(<ConfigEditor mode="advanced" />);
    await waitFor(() => screen.getByTestId("toggle-enable_compact"));
    fireEvent.click(screen.getByTestId("toggle-enable_compact"));
    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    expect(putSpy).toHaveBeenCalledWith(
      { log_level: "INFO", enable_compact: false },
      undefined,
    );
  });

  it("putConfig fails → show saveError", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    transport.putConfig.mockRejectedValue(new Error("server 500"));
    wrap(<ConfigEditor mode="advanced" />);
    const btn = await waitFor(() =>
      screen.getByTestId("toggle-enable_compact"),
    );
    fireEvent.click(btn);
    await waitFor(() => screen.getByText(/Save failed/));
    expect(screen.getAllByText(/server 500/).length).toBeGreaterThan(0);
  });
  it("splits owners and retains a committed restart when another owner fails", async () => {
    transport.getConfig.mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) => ({ ...field, owner: field.name === "enable_compact" ? "meiva_fleet" : null })),
    });
    const put = transport.putConfig.mockResolvedValueOnce(OK_RESULT(["log_level"], ["all"])).mockRejectedValueOnce(new Error("Fleet image changed"));
    await renderSettled();
    fireEvent.click(screen.getByTestId("toggle-enable_compact"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(put.mock.calls).toEqual([[{ log_level: "INFO" }, undefined], [{ enable_compact: false }, undefined]]);
    expect(await screen.findByText(/Save failed/)).toBeTruthy();
    expect(screen.getByText(/Fleet image changed/)).toBeTruthy();
    expect(screen.getByRole("status").textContent).toMatch(/All processes/);
  });

});

describe("ConfigPage enum select", () => {
  it("enum field renders a select of its choices with the current value", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="advanced" />);
    const select = await waitFor(() =>
      screen.getByTestId<HTMLSelectElement>("select-nudge_cadence"),
    );
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "once_per_compaction",
      "every_time",
    ]);
    expect(select.value).toBe("once_per_compaction");
  });

  it("selecting a choice PUTs the delta immediately", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    const putSpy = transport.putConfig
      .mockResolvedValue(OK_RESULT(["nudge_cadence"], ["agent"]));
    wrap(<ConfigEditor mode="advanced" />);
    const select = await waitFor(() =>
      screen.getByTestId<HTMLSelectElement>("select-nudge_cadence"),
    );
    fireEvent.change(select, { target: { value: "every_time" } });
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        { log_level: "INFO", nudge_cadence: "every_time" },
        undefined,
      ),
    );
  });

  it("shows a disabled not-set placeholder for an empty enum value", async () => {
    transport.getConfig.mockResolvedValue({
      ...VIEW,
      fields: VIEW.fields.map((field) =>
        field.name === "nudge_cadence" ? { ...field, current_value: null } : field,
      ),
    });
    wrap(<ConfigEditor mode="advanced" />);

    const select = await screen.findByTestId<HTMLSelectElement>("select-nudge_cadence");
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toBe("— not set —");
    expect(select.options[0].disabled).toBe(true);
  });

  it("enum field is read-only text (no select) on a remote machine view", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="machines" />);
    const machineSelect = await waitFor(() =>
      screen.getByLabelText<HTMLSelectElement>("Machine"),
    );
    fireEvent.change(machineSelect, { target: { value: AGENT_RUNNER } });

    await screen.findByText("Host label");
    expect(screen.queryByText("once_per_compaction")).toBeNull();
    expect(screen.queryByTestId("select-nudge_cadence")).toBeNull();
  });
});


describe("Settings scope", () => {
  it("general settings omit machine selection and technical configuration", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    wrap(<ConfigEditor mode="general" />);
    expect(await screen.findByTestId("config-default-model")).toBeTruthy();
    expect(screen.queryByLabelText("Machine")).toBeNull();
    expect(screen.queryByText("Log level")).toBeNull();
    expect(screen.queryByText("Host label")).toBeNull();
  });

  it("advanced search finds a field by its technical name without exposing host settings", async () => {
    transport.getConfig.mockResolvedValue(VIEW);
    await renderSettled();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "MEIVA_LOG_LEVEL" } });
    expect(screen.getByText("Log level")).toBeTruthy();
    expect(document.getElementById("config-field-max_retries")).toBeNull();
    expect(screen.queryByText("Host label")).toBeNull();
  });
});
