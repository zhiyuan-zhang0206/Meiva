import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import OpsPage from "./page";

afterEach(cleanup);

function wrap(ui: React.ReactElement) {
  return render(ui);
}

describe("Ops tab (Grafana link)", () => {
  it("renders the Metrics section sub-anchor", () => {
    wrap(<OpsPage />);
    expect(document.getElementById("ops-metrics")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Metrics (Grafana)" })).toBeTruthy();
  });

  it("renders the Alerts sub-anchor with a link to Grafana's active critical and error instances", () => {
    wrap(<OpsPage />);
    expect(document.getElementById("ops-alerts")).toBeTruthy();
    const link = screen.getByRole("link", { name: "Open active critical and error alerts in Grafana" });
    expect(link.getAttribute("href")).toBe(
      "http://localhost:8000/grafana/alerting/groups?alertState=active&queryString=severity%3D~%22critical%7Cerror%22&groupBy=alertname%2Cmachine",
    );
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("renders one link to the merged dashboard through the gateway proxy", () => {
    wrap(<OpsPage />);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(2);
    const link = screen.getByRole("link", { name: "Open the Meiva ops dashboard" });
    expect(link.getAttribute("href")).toBe(
      "http://localhost:8000/grafana/d/meiva-ops-main?from=now-24h&to=now",
    );
    expect(link.getAttribute("href")).toContain("/grafana/d/meiva-ops-main");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
  });

  it("does not render an iframe", () => {
    wrap(<OpsPage />);
    expect(document.querySelector("iframe")).toBeNull();
  });
});
