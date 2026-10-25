// meiva:note
// @vitest-environment happy-dom

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  BREAKPOINT_LG_PX,
  BREAKPOINT_MD_PX,
  BREAKPOINT_WIDE_PX,
  tierForWidth,
  useBreakpoint,
} from "./breakpoint";

const BREAKPOINT_XS_PX = 320;
const BREAKPOINT_SM_PX = 390;

const initialInnerWidth = window.innerWidth;

function resizeViewport(width: number): void {
  window.innerWidth = width;
  window.dispatchEvent(new Event("resize"));
}

beforeEach(() => {
  resizeViewport(BREAKPOINT_SM_PX);
});

afterEach(() => {
  resizeViewport(initialInnerWidth);
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
  it("defaults to narrow/mobile on a narrow viewport", () => {
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
    expect(result.current.isWide).toBe(false);
  });

  it("isNarrow = NOT matching the md query; isLarge = matching the lg query", () => {
    resizeViewport(BREAKPOINT_MD_PX);
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current.isNarrow).toBe(false);
    expect(result.current.isLarge).toBe(false);

    act(() => resizeViewport(BREAKPOINT_LG_PX));
    expect(result.current.isNarrow).toBe(false);
    expect(result.current.isLarge).toBe(true);

    act(() => resizeViewport(BREAKPOINT_SM_PX));
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
  });

  it("isWide follows the 1280px query without changing the xl tier", () => {
    resizeViewport(BREAKPOINT_LG_PX);
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current.isLarge).toBe(true);
    expect(result.current.isWide).toBe(false);

    act(() => resizeViewport(BREAKPOINT_WIDE_PX));
    expect(result.current.isWide).toBe(true);
    expect(tierForWidth(BREAKPOINT_WIDE_PX - 1)).toBe("xl");
    expect(tierForWidth(BREAKPOINT_WIDE_PX)).toBe("xl");
  });

  it("tier follows the real viewport width after mount", () => {
    resizeViewport(BREAKPOINT_SM_PX);
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current.tier).toBe("md");
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
  });
});
