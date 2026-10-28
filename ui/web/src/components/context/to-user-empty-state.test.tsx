import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/transport/api";

import en from "../../../messages/en";
import zh from "../../../messages/zh";
import { ToUserDocumentView } from "./to-user-document";

const clients = new Set<QueryClient>();
const originalUrl = window.location.href;
const originalHistory: unknown = window.history.state;

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  vi.spyOn(api, "getToUser").mockImplementation(() => new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  window.history.replaceState(originalHistory, "", originalUrl);
  vi.restoreAllMocks();
});

function renderEmpty(messages: typeof en, locale: "en" | "zh") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  return renderWithBrowser(
    <QueryClientProvider client={qc}>
      <NextIntlClientProvider locale={locale} messages={{ toUser: messages.toUser }}>
        <ToUserDocumentView
          agentId={7}
          document={{ content: null, version: null, modified_at: null, legacy_html: false, resources: {} }}
          failure={null}
          pending={false}
          refresh={vi.fn()}
          mobile={false}
        />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe("To user empty state", () => {
  it("renders exactly `No content` — no sentence, no period", () => {
    renderEmpty(en, "en");
    const empty = screen.getByText("No content");
    expect(empty.textContent).toBe("No content");
  });

  it("keeps one literal for both locales instead of a per-locale branch", () => {
    expect(en.toUser.empty).toBe("No content");
    expect(zh.toUser.empty).toBe("No content");
    const view = renderEmpty(zh, "zh");
    expect(screen.getByText("No content")).toBeTruthy();
    expect(api.getToUser).not.toHaveBeenCalled();
    const { browser } = view;
    expect(browser.actions.traversals).toBe(1);
    view.unmount();
    expect(browser.actions.traversals).toBe(0);
    for (const client of clients) {
      expect(client.getQueryCache().getAll().every((query) => query.getObserversCount() === 0)).toBe(true);
    }
  });
});
