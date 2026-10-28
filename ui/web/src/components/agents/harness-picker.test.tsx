import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { harnessRows, type HarnessAvailability } from "@/lib/harness/harness";
import { HarnessPicker } from "./harness-picker";
import { renderWithApi as rtlRender } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getHarnessAvailability.mockImplementation(() => new Promise(() => undefined));
});


const clients = new Set<QueryClient>();
let rows: HarnessAvailability[] = [];

function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  qc.setQueryData(["harness-availability", "local"], rows);
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  vi.clearAllMocks();
});

function row(
  harness: HarnessAvailability["harness"],
  status: HarnessAvailability["status"],
  launchable: boolean,
  reason = "",
): HarnessAvailability {
  return { harness, status, reason, launchable, billing: harness === "meiva" ? "api" : "subscription" };
}

describe("harnessRows", () => {
  it("keeps the built-in runtime usable when the gateway cannot report at all", () => {
    const rows = harnessRows([]);
    expect(rows.find((r) => r.harness === "meiva")).toMatchObject({ status: "available", launchable: true });
    expect(rows.filter((r) => r.harness !== "meiva").every((r) => r.status === "unknown")).toBe(true);
  });

  it("never reports an unprobed harness as unavailable at the data layer", () => {
    const rows = harnessRows([row("codex", "available", false)]);
    expect(rows.find((r) => r.harness === "codex")).toMatchObject({ status: "available", launchable: false });
    expect(rows.find((r) => r.harness === "claude_code")?.status).toBe("unknown");
    expect(rows.find((r) => r.harness === "kimi_code")?.status).toBe("unknown");
  });
});

describe("HarnessPicker", () => {
  // meiva:note
  // The row answers one question with one word: can this machine start an agent
  // on it here. An installed-but-unlaunchable harness reads `unavailable` like a
  // missing one, and the probe's diagnostic sentence never reaches the screen.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  it("leaves a usable row as plain text and never shows the probe's reason", async () => {
    rows = [
      row("meiva", "available", true, "built-in runtime; needs no external CLI or login"),
      row("codex", "unavailable", false, "no codex executable on PATH or in ~/.local/bin"),
    ];
    render(<HarnessPicker machine="local" value="meiva" onChange={() => undefined} />);

    fireEvent.click(screen.getByLabelText("Harness"));

    const meivaRow = (await screen.findAllByText("Meiva")).find((el) => el.closest("li"));
    expect(meivaRow?.closest("button")?.textContent).toBe("Meiva");
    expect(screen.queryByText("no codex executable on PATH or in ~/.local/bin")).toBeNull();
    expect(screen.queryByText(/unknown/i)).toBeNull();
  });

  it("greys an unusable row and adds one lowercase word", async () => {
    rows = [
      row("meiva", "available", true),
      row("claude_code", "available", false, "credentials file found"),
      row("codex", "unavailable", false),
    ];
    render(<HarnessPicker machine="local" value="meiva" onChange={() => undefined} />);

    fireEvent.click(screen.getByLabelText("Harness"));

    const claude = await screen.findByText("Claude Code");
    const button = claude.closest("button");
    expect(button?.disabled).toBe(true);
    expect(button?.textContent).toBe("Claude Codeunavailable");
    const codex = (await screen.findByText("Codex")).closest("button");
    expect(codex?.disabled).toBe(true);
    expect(codex?.textContent).toBe("Codexunavailable");
    expect(transport.getHarnessAvailability).not.toHaveBeenCalled();
  });

  it("selects a harness this machine can start", async () => {
    rows = [row("meiva", "available", true), row("claude_code", "available", true)];
    const onChange = vi.fn();
    render(<HarnessPicker machine="local" value="meiva" onChange={onChange} />);

    fireEvent.click(screen.getByLabelText("Harness"));
    fireEvent.click(await screen.findByText("Claude Code"));

    expect(onChange).toHaveBeenCalledWith("claude_code");
    expect(transport.getHarnessAvailability).not.toHaveBeenCalled();
  });
});
