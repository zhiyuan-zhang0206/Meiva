// /plugin/<plugin>/<path> — the route that frames a plugin-served page.
//
// Two things here are logic rather than markup: the frame must point at the
// path the URL names (not at whatever the declaration said), and the heading
// must find the declared label for that page even though the console route
// carries no trailing slash while the declaration does.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";


import PluginPage from "./page";
import { StreamHubProvider } from "@/lib/transport/transport-context";
import { renderWithApi as render } from "@/test-support/render";
import { fakeStreamHub } from "@/test-support/fake-stream";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});


function renderRoute(plugin: string, path?: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // meiva:note
  // The route mounts the app shell's ErrorBoundary, which reports to the page's diagnostic
  // sink; a route test mounts the hub the shell would have mounted.
  return render(
    <StreamHubProvider hub={fakeStreamHub().hub}>
      <QueryClientProvider client={qc}>
        <PluginPage params={Promise.resolve({ plugin, path })} />
      </QueryClientProvider>
    </StreamHubProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  transport.getUiContributions.mockResolvedValue({
    themes: [],
    stats: [],
    nav: [
      {
        plugin: "board",
        location: "sidebar",
        label: "Task board",
        icon: "kanban",
        page: "board/",
      },
    ],
  });
});

afterEach(cleanup);

describe("plugin page route", () => {
  it("frames the page the URL names and labels it from the declaration", async () => {
    const { container } = renderRoute("board", ["board"]);

    // The declaration says `board/`; the route carries `board` — the same page.
    await waitFor(() => expect(screen.getByText("Task board")).toBeTruthy());
    const iframe = container.querySelector("iframe");
    expect(iframe?.getAttribute("src")).toBe(
      "http://localhost:8000/api/plugin-ui/board/board",
    );
    // Provenance is on screen next to the label.
    expect(screen.getByText("board")).toBeTruthy();
  });

  it("frames a deep path no nav entry declares, labelled by the plugin", async () => {
    const { container } = renderRoute("board", ["reports", "q3.html"]);

    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    expect(container.querySelector("iframe")?.getAttribute("src")).toBe(
      "http://localhost:8000/api/plugin-ui/board/reports/q3.html",
    );
    expect(screen.getByRole("heading").textContent).toBe("board");
  });

  it("frames the mount root when the URL carries no path", async () => {
    const { container } = renderRoute("board");

    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    expect(container.querySelector("iframe")?.getAttribute("src")).toBe(
      "http://localhost:8000/api/plugin-ui/board/",
    );
  });
});
