import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeColorSync } from "./theme-color-sync";

let mockMode = "dark";
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme: mockMode, theme: mockMode, setTheme: vi.fn() }),
}));

// meiva:note
// Next renders one media-scoped theme-color per scheme for the first paint; the
// sync overwrites both so an in-app override beats the OS preference.
// meiva:link ui/web/src/docs/frontend.meiva.okf.md
function metas(): HTMLMetaElement[] {
  return [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')];
}
function palette(color: string) {
  document.documentElement.style.setProperty("--theme-color", color);
}

beforeEach(() => {
  mockMode = "dark";
  for (const meta of metas()) meta.remove();
  for (const [media, color] of [
    ["(prefers-color-scheme: light)", "#ffffff"],
    ["(prefers-color-scheme: dark)", "#222222"],
  ]) {
    const meta = document.createElement("meta");
    meta.name = "theme-color";
    meta.media = media;
    meta.content = color;
    document.head.append(meta);
  }
});

afterEach(() => {
  cleanup();
  for (const meta of metas()) meta.remove();
  document.documentElement.style.removeProperty("--theme-color");
});

describe("ThemeColorSync", () => {
  it("copies the palette's chrome colour onto every theme-color meta", () => {
    palette("#222222");
    render(<ThemeColorSync />);
    expect(metas().map((m) => m.content)).toEqual(["#222222", "#222222"]);
  });

  it("follows an in-app theme override on the next theme change", () => {
    palette("#222222");
    const { rerender } = render(<ThemeColorSync />);
    expect(metas()[0].content).toBe("#222222");

    mockMode = "light";
    palette("#ffffff");
    act(() => rerender(<ThemeColorSync />));
    expect(metas().map((m) => m.content)).toEqual(["#ffffff", "#ffffff"]);
  });

  it("leaves the metas alone when the palette declares no chrome colour", () => {
    render(<ThemeColorSync />);
    expect(metas().map((m) => m.content)).toEqual(["#ffffff", "#222222"]);
  });
});
