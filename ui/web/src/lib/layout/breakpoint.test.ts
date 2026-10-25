// meiva:note
// @vitest-environment happy-dom

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  BREAKPOINT_LG_PX,
  BREAKPOINT_MD_PX,
  BREAKPOINT_WIDE_PX,
  tierForWidth,
  useBreakpoint,
} from "./breakpoint";

const BREAKPOINT_XS_PX = 320;
const BREAKPOINT_SM_PX = 390;

const mdMatches = vi.fn<() => boolean>(() => false);
const lgMatches = vi.fn<() => boolean>(() => false);
const wideMatches = vi.fn<() => boolean>(() => false);

function matchMediaQuery(query: string): boolean {
  if (query === `(min-width: ${BREAKPOINT_MD_PX}px)`) return mdMatches();
  if (query === `(min-width: ${BREAKPOINT_LG_PX}px)`) return lgMatches();
  if (query === `(min-width: ${BREAKPOINT_WIDE_PX}px)`) return wideMatches();
  throw new Error(`Unexpected media query: ${query}`);
}

beforeEach(() => {
  mdMatches.mockReturnValue(false);
  lgMatches.mockReturnValue(false);
  wideMatches.mockReturnValue(false);
});

describe("tierForWidth — 320/390/768/lg boundaries", () => {
  it("maps every tier band to its name", () => {
    expect(tierForWidth(BREAKPOINT_XS_PX - 1)).toBe("xs");
    expect(tierForWidth(BREAKPOINT_XS_PX)).toBe("sm");
    expect(tierForWidth(BREAKPOINT_SM_PX - 1)).toBe("sm");
    expect(tierForWidth(BREAKPOINT_SM_PX)).toBe("md");
    expect(tierForWidth(BREAKPOINT_MD_PX - 1)).toBe("md");
    expect(tierForWidth(BREAKPOINT_MD_PX)).toBe("lg");
    expect(tierForWidth(BREAKPOINT_LG_PX - 1)).toBe("lg");
    expect(tierForWidth(BREAKPOINT_LG_PX)).toBe("xl");
    expect(tierForWidth(1920)).toBe("xl");
  });
});

describe("useBreakpoint", () => {
  it("defaults to narrow/mobile before mount (SSR-safe)", () => {
    const { result } = renderHook(() => useBreakpoint(matchMediaQuery));
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
    expect(result.current.isWide).toBe(false);
  });

  it("isNarrow = NOT matching the md query; isLarge = matching the lg query", () => {
    mdMatches.mockReturnValue(true);
    lgMatches.mockReturnValue(false);
    const { result, rerender } = renderHook(() => useBreakpoint(matchMediaQuery));
    rerender();
    expect(result.current.isNarrow).toBe(false);
    expect(result.current.isLarge).toBe(false);

    lgMatches.mockReturnValue(true);
    rerender();
    expect(result.current.isNarrow).toBe(false);
    expect(result.current.isLarge).toBe(true);

    mdMatches.mockReturnValue(false);
    lgMatches.mockReturnValue(false);
    rerender();
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
  });

  it("isWide follows the 1280px query without changing the xl tier", () => {
    mdMatches.mockReturnValue(true);
    lgMatches.mockReturnValue(true);
    wideMatches.mockReturnValue(false);
    const { result, rerender } = renderHook(() => useBreakpoint(matchMediaQuery));
    expect(result.current.isLarge).toBe(true);
    expect(result.current.isWide).toBe(false);

    wideMatches.mockReturnValue(true);
    rerender();
    expect(result.current.isWide).toBe(true);
    expect(tierForWidth(BREAKPOINT_WIDE_PX - 1)).toBe("xl");
    expect(tierForWidth(BREAKPOINT_WIDE_PX)).toBe("xl");
  });

  it("tier follows the real viewport width after mount", () => {
    const width = 390;
    const orig = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: width, writable: true });
    try {
      const { result } = renderHook(() => useBreakpoint(matchMediaQuery));
      expect(result.current.tier).toBe("md");
      expect(result.current.isNarrow).toBe(true); // 390 < 768
      expect(result.current.isLarge).toBe(false);
    } finally {
      Object.defineProperty(window, "innerWidth", { value: orig, writable: true });
    }
  });
});
