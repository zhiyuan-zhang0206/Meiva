import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import en from "../../../messages/en";
import zh from "../../../messages/zh";
import { ToUserDocumentView } from "./to-user-document";

function renderEmpty(messages: typeof en, locale: "en" | "zh") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NextIntlClientProvider locale={locale} messages={{ toUser: messages.toUser }}>
        <ToUserDocumentView
          agentId={7}
          document={{ content: null, version: null, modified_at: null, legacy_html: false, resources: {} }}
          failed={false}
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
    renderEmpty(zh, "zh");
    expect(screen.getByText("No content")).toBeTruthy();
  });
});
