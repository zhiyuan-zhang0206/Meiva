// useMediaQuery — SSR-safe media query reads through useSyncExternalStore.
//
// Two contracts matter here: the server snapshot answers false (the
// mobile-first frame during SSR and hydration), and a CLIENT mount reads the
// real match in its first render — no post-mount flip (that flip painted a
// wrong frame whenever a commit and the passive flush straddled a rendering
// pass).

// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, render } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useMediaQuery } from "./use-media-query";

const initialInnerWidth = window.innerWidth;

function resizeViewport(width: number): void {
  window.innerWidth = width;
  window.dispatchEvent(new Event("resize"));
}

beforeEach(() => {
  resizeViewport(390);
});

afterEach(() => {
  cleanup();
  resizeViewport(initialInnerWidth);
});

function Probe() {
  return <span data-testid="value">{String(useMediaQuery("(min-width: 768px)"))}</span>;
}

describe("useMediaQuery", () => {
  it("answers the server snapshot (false) during SSR", () => {
    resizeViewport(1024); // the "server" has no viewport; the snapshot wins
    expect(renderToString(<Probe />)).toContain(">false<");
  });

  it("reads the live match on the first client render", () => {
    resizeViewport(1024);
    const { getByTestId } = render(<Probe />);
    expect(getByTestId("value").textContent).toBe("true");
  });

  it("follows change events", () => {
    const { getByTestId } = render(<Probe />);
    expect(getByTestId("value").textContent).toBe("false");

    act(() => {
      resizeViewport(1024);
    });
    expect(getByTestId("value").textContent).toBe("true");
  });
});
