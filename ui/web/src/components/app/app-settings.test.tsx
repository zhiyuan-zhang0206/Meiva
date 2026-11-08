import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AppSettingsEntry,
  AppUpdateBanner,
  type AppUpdatePrompt,
  readAppBridge,
} from "./app-settings";
import type { HostShellPayload } from "./bridge";

interface HostWindow {
  __MEIVA_APP__?: HostShellPayload;
}

afterEach(() => {
  delete (window as unknown as HostWindow).__MEIVA_APP__;
});

describe("readAppBridge", () => {
  it("stays null outside the Meiva app", () => {
    expect(readAppBridge()).toBeNull();
  });

  it("asks the app to open its settings page through the host bridge", async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    (window as unknown as HostWindow).__MEIVA_APP__ = { invoke };
    const bridge = readAppBridge();
    expect(bridge).not.toBeNull();
    await bridge?.openSettings();
    expect(invoke).toHaveBeenCalledWith("app_open_settings");
  });
});

describe("AppSettingsEntry", () => {
  it("invokes the settings command on click", async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    (window as unknown as HostWindow).__MEIVA_APP__ = { invoke };
    render(<AppSettingsEntry bridge={readAppBridge()!} />);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("app_open_settings"));
  });

  it("surfaces a rejected invocation", async () => {
    const invoke = vi.fn().mockRejectedValue(new Error("denied"));
    (window as unknown as HostWindow).__MEIVA_APP__ = { invoke };
    render(<AppSettingsEntry bridge={readAppBridge()!} />);
    fireEvent.click(screen.getByRole("button"));
    expect(await screen.findByRole("status")).toBeTruthy();
  });
});

describe("AppUpdateBanner", () => {
  it("renders nothing when no host update is available", () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    (window as unknown as HostWindow).__MEIVA_APP__ = { invoke, update: null };
    const { container } = render(<AppUpdateBanner />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the available update in normal document flow and wires install and dismiss", () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    const install = vi.fn();
    const dismiss = vi.fn();
    const update: AppUpdatePrompt = {
      status: "available",
      versionName: "1.0.0",
      versionCode: 4003,
      text: "Meiva 1.0.0 (build 4003) is available",
      actionLabel: "Update",
      downloadPage: "https://example.com/download",
      install,
      dismiss,
    };
    (window as unknown as HostWindow).__MEIVA_APP__ = { invoke };
    render(<AppUpdateBanner />);
    expect(screen.queryByRole("region", { name: "App update" })).toBeNull();

    act(() => {
      (window as unknown as HostWindow).__MEIVA_APP__ = { invoke, update };
      window.dispatchEvent(new Event("meiva-app-update"));
    });

    expect(screen.getByText("Meiva 1.0.0 (build 4003) is available")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(install).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    expect(dismiss).toHaveBeenCalledTimes(1);
  });
});
