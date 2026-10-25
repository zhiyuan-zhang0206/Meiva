// LanguageProvider — the settings→next-intl bridge. Uses the REAL next-intl
// module (unlike the global test double in vitest.setup.ts, which serves
// components rendered without a provider): this test verifies the actual
// provider wiring — locale + messages handed to NextIntlClientProvider, and
// the <html lang> sync effect.
//
// The settings store is the shared mock (user-settings-mock.ts), so flipping
// display.language re-renders consumers immediately — the same path the
// production optimistic update takes.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useTranslations } from "next-intl";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { USER_SETTING_DEFAULTS } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import type { Api } from "@/lib/transport/api";
import { ApiProvider } from "@/lib/transport/transport-context";
import { LanguageProvider } from "./language-provider";

const defaultSettings = (): Record<string, unknown> => ({
  ...USER_SETTING_DEFAULTS,
});

const cacheOnlyApi = (): Api =>
  new Proxy({} as Api, {
    get(_target, prop: string) {
      throw new Error(`unexpected Api.${prop} call in cache-seeded test`);
    },
  });

let queryClient: QueryClient;

function setCachedSetting(key: string, value: unknown): void {
  queryClient.setQueryData<Record<string, unknown>>(
    SETTINGS_QUERY_KEY,
    (old: Record<string, unknown> | undefined) => ({
      ...(old ?? defaultSettings()),
      [key]: value,
    }),
  );
}

function renderWithProviders() {
  return render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider api={cacheOnlyApi()}>
        <LanguageProvider>
          <Probe />
        </LanguageProvider>
      </ApiProvider>
    </QueryClientProvider>,
  );
}

// A consumer that reads a key from the "spawn" namespace — renders the
// English string when locale=en, the Chinese string when locale=zh.
function Probe() {
  const t = useTranslations("spawn");
  return <span>{t("spawnAgent")}</span>;
}

afterEach(() => {
  cleanup();
  document.documentElement.lang = "en";
});

beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  queryClient.setQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY, defaultSettings());
});

describe("LanguageProvider", () => {
  it("defaults to English when the language setting is absent", () => {
    renderWithProviders();
    expect(screen.getByText("Spawn agent")).toBeTruthy();
    expect(document.documentElement.lang).toBe("en");
  });

  it("renders the Chinese catalog when display.language=zh and syncs <html lang>", () => {
    setCachedSetting("display.language", "zh");
    renderWithProviders();
    expect(screen.getByText("\u521b\u5efa agent")).toBeTruthy();
    expect(document.documentElement.lang).toBe("zh-CN");
  });

  it("switches locale live when the setting changes", async () => {
    const { rerender } = renderWithProviders();
    expect(screen.getByText("Spawn agent")).toBeTruthy();

    act(() => {
      setCachedSetting("display.language", "zh");
    });

    await waitFor(() => {
      expect(screen.getByText("\u521b\u5efa agent")).toBeTruthy();
    });
    expect(document.documentElement.lang).toBe("zh-CN");
    rerender(
      <QueryClientProvider client={queryClient}>
        <ApiProvider api={cacheOnlyApi()}>
          <LanguageProvider>
            <Probe />
          </LanguageProvider>
        </ApiProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByText("\u521b\u5efa agent")).toBeTruthy();
  });
});
