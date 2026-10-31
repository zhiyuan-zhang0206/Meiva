// Skills section tests — tri-state (loading / error / empty) + the table
// (name, source layer, enabled) + the natural-language install entry, which
// stays usable through every tri-state. Renders <SkillsPage /> directly;
// useSectionVisible defaults true so the query enables. Mock at the api layer.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { fakeNavigation, navigationWrapper } from "@/lib/browser/navigation/testing";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SkillsView } from "@/lib/contracts/types";

import SkillsPage from "./page";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});


// meiva:note
// The page navigates through the app's navigation port, not Next's router: the moves it asked for
// are read off the fake that stands in for the port.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
const navigation = fakeNavigation();

afterEach(cleanup);
beforeEach(() => {
  vi.restoreAllMocks();
  navigation.moves.length = 0;
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  // meiva:note
  // The page carries timeline cards, which age their stamps on the page's wall clock: the provider
  // the app mounts is what `useWallNow` reads, and without it the render throws.
  // meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
  const Page = navigationWrapper(navigation);
  return render(
    <WallClockProvider wallNow={() => Date.now()}>
      <Page>
        <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
      </Page>
    </WallClockProvider>,
  );
}

const SKILLS: SkillsView = {
  skills: [
    { name: "alpha", layer: "core", enabled: true, modified_locally: false },
    { name: "beta", layer: "plugin", enabled: true, modified_locally: false },
    { name: "gamma", layer: "machine", enabled: false, modified_locally: false },
    { name: "stray", layer: "untracked", enabled: false, modified_locally: false },
  ],
};

describe("SkillsPage", () => {
  it("loading shows a spinner", () => {
    transport.getSkills.mockReturnValue(new Promise(() => undefined));
    const { container } = wrap(<SkillsPage />);
    expect(container.querySelector(".animate-spin")).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    transport.getSkills.mockRejectedValue(new Error("boom 500"));
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/Couldn't load skills/));
    expect(screen.queryByText(/boom 500/)).toBeNull();
  });

  it("empty → (no skills installed)", async () => {
    transport.getSkills.mockResolvedValue({ skills: [] });
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/no skills installed/));
  });

  it("renders each skill with its layer + enabled", async () => {
    transport.getSkills.mockResolvedValue(SKILLS);
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText("alpha"));
    // names
    for (const n of ["alpha", "beta", "gamma", "stray"]) {
      expect(screen.getByText(n)).toBeTruthy();
    }
    // layer badges
    expect(screen.getByText("Core")).toBeTruthy();
    expect(screen.getByText("Plugin")).toBeTruthy();
    expect(screen.getByText("Machine")).toBeTruthy();
    expect(screen.getByText("Untracked")).toBeTruthy();
    // enabled column: alpha/beta enabled (checked), gamma/stray disabled (unchecked)
    const toggles = screen.getAllByRole("switch");
    expect(toggles.length).toBe(4);
    // alpha should be checked (enabled)
    expect(toggles[0].getAttribute("data-state")).toBe("checked");
    // gamma should be unchecked (disabled)
    expect(toggles[2].getAttribute("data-state")).toBe("unchecked");
    // stray (untracked) should be disabled
    expect(toggles[3].hasAttribute("disabled")).toBe(true);
  });

  it("install entry posts kind=skill and opens the installer's conversation", async () => {
    transport.getSkills.mockResolvedValue({ skills: [] });
    const draft = transport.draftPackage.mockResolvedValue({ agent_id: 55 });
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/no skills installed/));

    fireEvent.change(screen.getByPlaceholderText(/Describe a skill you want/), {
      target: { value: "write release notes the way we do" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install a skill" }));

    await waitFor(() =>
      expect(draft).toHaveBeenCalledWith("skill", "write release notes the way we do"),
    );
    await waitFor(() => expect(navigation.moves).toContainEqual({ kind: "push", href: "/" }));
  });

  it("install entry survives a failed list read", async () => {
    transport.getSkills.mockRejectedValue(new Error("boom 500"));
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/Couldn't load skills/));
    expect(screen.getByRole("button", { name: "Find and install a skill" })).toBeTruthy();
  });
});
