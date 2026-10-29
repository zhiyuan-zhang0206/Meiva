// meiva:note
// useMediaQuery — SSR-safe media query reads through useSyncExternalStore. Two contracts matter
// here: the server snapshot answers false (the mobile-first frame during SSR and hydration), and a
// CLIENT mount reads the real match in its first render — no post-mount flip (that flip painted a
// wrong frame whenever a commit and the passive flush straddled a rendering pass). The match comes
// from the browser port, so a test answers it by value.
//
// lib/ may not import src/test-support/ (test-support already reaches lib), so a hook test here
// mounts this page by hand; component tests use `@/test-support/render`, the same thing with less
// boilerplate.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, render } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";
import { useMediaQuery } from "./use-media-query";

function page(browser: FakeBrowser) {
  return ({ children }: { children: React.ReactNode }) => (
    <BrowserProvider browser={browser}>{children}</BrowserProvider>
  );
}

const QUERY = "(min-width: 768px)";

afterEach(cleanup);

function Probe({ serverMatches }: { serverMatches?: boolean }) {
  return <span data-testid="value">{String(useMediaQuery(QUERY, serverMatches))}</span>;
}

describe("useMediaQuery", () => {
  it("answers the server snapshot (false) during SSR, whatever the page says", () => {
    const browser = fakeBrowser({ media: { [QUERY]: true } });
    const html = renderToString(
      <BrowserProvider browser={browser}>
        <Probe />
      </BrowserProvider>,
    );
    expect(html).toContain(">false<");
  });

  // meiva:note
  // A caller whose first frame is not the mobile one names it: the argument is the server snapshot
  // only, so the client still reads the real match on its first render.
  it("answers the snapshot a caller names, and still reads the real match once mounted", () => {
    const browser = fakeBrowser({ media: { [QUERY]: false } });
    const html = renderToString(
      <BrowserProvider browser={browser}>
        <Probe serverMatches={true} />
      </BrowserProvider>,
    );
    expect(html).toContain(">true<");
    const { getByTestId } = render(<Probe serverMatches={true} />, { wrapper: page(browser) });
    expect(getByTestId("value").textContent).toBe("false");
  });

  it("reads the live match on the first client render", () => {
    const { getByTestId } = render(<Probe />, {
      wrapper: page(fakeBrowser({ media: { [QUERY]: true } })),
    });
    expect(getByTestId("value").textContent).toBe("true");
  });

  it("follows a change the page reports", () => {
    const browser = fakeBrowser({ media: { [QUERY]: false } });
    const { getByTestId } = render(<Probe />, { wrapper: page(browser) });
    expect(getByTestId("value").textContent).toBe("false");

    act(() => {
      browser.answerMedia(QUERY, true);
    });
    expect(getByTestId("value").textContent).toBe("true");
  });
});
