import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import en from "../../../messages/en";
import { ToUserDocumentView } from "./to-user-document";
import { WallClockProvider } from "@/lib/transport/transport-context";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});

const clients = new Set<QueryClient>();
const originalUrl = window.location.href;
const originalHistory: unknown = window.history.state;

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  transport.getToUser.mockImplementation(() => new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  window.history.replaceState(originalHistory, "", originalUrl);
  vi.restoreAllMocks();
});

function renderEmpty() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  return renderWithBrowser(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>
        <NextIntlClientProvider locale="en" messages={{ toUser: en.toUser }}>
          <ToUserDocumentView
            agentId={7}
            document={{ content: null, version: null, modified_at: null, legacy_html: false, resources: {} }}
            failure={null}
            pending={false}
          />
        </NextIntlClientProvider>
      </QueryClientProvider>
    </WallClockProvider>,
  );
}

describe("To user empty state", () => {
  it("renders exactly `No content` — no sentence, no period", () => {
    renderEmpty();
    const empty = screen.getByText("No content");
    expect(empty.textContent).toBe("No content");
  });

  it("renders the empty state without fetching or traversing", () => {
    expect(en.toUser.empty).toBe("No content");
    const view = renderEmpty();
    expect(screen.getByText("No content")).toBeTruthy();
    expect(transport.getToUser).not.toHaveBeenCalled();
    const { browser } = view;
    expect(browser.actions.traversals).toBe(0);
    view.unmount();
    for (const client of clients) {
      expect(client.getQueryCache().getAll().every((query) => query.getObserversCount() === 0)).toBe(true);
    }
  });
});
