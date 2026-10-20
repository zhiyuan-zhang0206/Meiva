import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { harnessRows, type HarnessAvailability } from "@/lib/harness/harness";

let rows: HarnessAvailability[] = [];
vi.mock("@/lib/harness/use-harness-availability", () => ({
  useHarnessAvailability: () => ({ data: rows }),
}));

import { HarnessPicker } from "./harness-picker";

afterEach(cleanup);

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
  });
});

describe("HarnessPicker", () => {
  // meiva:note
  // The row answers one question with one word: can this machine start an agent
  // on it here. An installed-but-unlaunchable harness reads `unavailable` like a
  // missing one, and the probe's diagnostic sentence never reaches the screen.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
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
  });

  it("selects a harness this machine can start", async () => {
    rows = [row("meiva", "available", true), row("claude_code", "available", true)];
    const onChange = vi.fn();
    render(<HarnessPicker machine="local" value="meiva" onChange={onChange} />);

    fireEvent.click(screen.getByLabelText("Harness"));
    fireEvent.click(await screen.findByText("Claude Code"));

    expect(onChange).toHaveBeenCalledWith("claude_code");
  });
});
