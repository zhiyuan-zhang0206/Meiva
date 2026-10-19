import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { harnessRows, type HarnessAvailability } from "@/lib/harness/harness";

let rows: HarnessAvailability[] = [];
vi.mock("@/lib/harness/use-harness-availability", () => ({
  useHarnessAvailability: () => ({ data: rows }),
}));

import { HarnessPicker } from "./harness-picker";

afterEach(cleanup);

function row(harness: HarnessAvailability["harness"], status: HarnessAvailability["status"], launchable: boolean, reason = ""): HarnessAvailability {
  return { harness, status, reason, launchable };
}

describe("harnessRows", () => {
  it("keeps the built-in runtime usable when the gateway cannot report at all", () => {
    const rows = harnessRows([]);
    expect(rows.find((r) => r.harness === "meiva")).toMatchObject({ status: "available", launchable: true });
    expect(rows.filter((r) => r.harness !== "meiva").every((r) => r.status === "unknown")).toBe(true);
  });

  it("never reports an unprobed harness as unavailable", () => {
    const rows = harnessRows([row("codex", "available", false)]);
    expect(rows.find((r) => r.harness === "codex")).toMatchObject({ status: "available", launchable: false });
    expect(rows.find((r) => r.harness === "claude_code")?.status).toBe("unknown");
  });
});

describe("HarnessPicker", () => {
  it("disables an unavailable harness and shows the probe's reason", async () => {
    rows = [row("meiva", "available", true), row("codex", "unavailable", false, "codex binary not on PATH")];
    render(<HarnessPicker machine="local" value="meiva" onChange={() => undefined} />);

    fireEvent.click(screen.getByLabelText("Harness"));

    const codex = await screen.findByText("Codex");
    const button = codex.closest("button");
    expect(button?.disabled).toBe(true);
    expect(button?.getAttribute("title")).toBe("codex binary not on PATH");
  });

  it("lets an unprovable harness be picked, since only its start is refused", async () => {
    rows = [row("meiva", "available", true), row("codex", "unknown", false, "login state unknown")];
    const onChange = vi.fn();
    render(<HarnessPicker machine="local" value="meiva" onChange={onChange} />);

    fireEvent.click(screen.getByLabelText("Harness"));
    fireEvent.click(await screen.findByText("Codex"));

    expect(onChange).toHaveBeenCalledWith("codex");
  });

  it("selects an available harness", async () => {
    rows = [row("meiva", "available", true), row("claude_code", "available", true)];
    const onChange = vi.fn();
    render(<HarnessPicker machine="local" value="meiva" onChange={onChange} />);

    fireEvent.click(screen.getByLabelText("Harness"));
    fireEvent.click(await screen.findByText("Claude Code"));

    expect(onChange).toHaveBeenCalledWith("claude_code");
  });

  it("says a harness cannot be started yet instead of letting it look usable", async () => {
    rows = [row("meiva", "available", true), row("codex", "available", false)];
    render(<HarnessPicker machine="local" value="codex" onChange={() => undefined} />);

    fireEvent.click(screen.getByLabelText("Harness"));

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("cannot start a session yet"));
  });
});
