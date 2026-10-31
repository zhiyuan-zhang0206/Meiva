import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FleetPageView } from "./page";

// meiva:note
// The supervision surface is a stand-in and the navigator a spy: the view takes both as required
// props, so this file covers the landmark and the boundary it owns.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
function FleetSurface() {
  return <section aria-label="Fleet supervision surface" />;
}

describe("FleetPage", () => {
  it("provides the primary landmark and mounts the fleet surface", () => {
    render(
      <FleetPageView
        FleetView={FleetSurface}
        navigate={vi.fn()}
        diagnostics={{ warn: vi.fn(), error: vi.fn() }}
      />,
    );

    const main = screen.getByRole("main");
    expect(main.id).toBe("main-content");
    expect(main.classList.contains("flex-1")).toBe(true);
    expect(screen.getByRole("region", { name: "Fleet supervision surface" })).toBeTruthy();
  });
});
