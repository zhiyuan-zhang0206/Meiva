// meiva:note
// The heading is part of the route's first paint; the body mounts one frame
// later, which is what keeps the route transition cheap.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ControlSection } from "./_section";

describe("ControlSection deferred body", () => {
  it("paints the heading with the route and mounts the body after it", async () => {
    render(
      <ControlSection id="config" label="Config">
        <div>CONFIG_BODY</div>
      </ControlSection>,
    );
    expect(screen.getByText("Config")).toBeTruthy();
    expect(screen.queryByText("CONFIG_BODY")).toBeNull();
    await waitFor(() => expect(screen.getByText("CONFIG_BODY")).toBeTruthy());
  });

  it("keeps the anchor on the section element while the body is still deferred", () => {
    const { container } = render(
      <ControlSection id="schedules" label="Schedules">
        <div>SCHEDULES_BODY</div>
      </ControlSection>,
    );
    expect(container.querySelector("#schedules")).toBeTruthy();
  });
});
