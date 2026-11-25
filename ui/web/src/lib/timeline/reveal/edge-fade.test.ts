// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearEdgeFade, edgeFadeCss, paintEdgeFade, settleEdgeFade, type HighlightHost, type HighlightLike } from "./edge-fade";
import { EDGE_FADE_SETTLE_MS, EDGE_FADE_STEP_OPACITY, EDGE_FADE_TAIL_GRAPHEMES } from "./constants";

class FakeHost implements HighlightHost {
  readonly highlights = new Map<string, Set<Range>>();
  readonly sheets: string[] = [];
  highlight(name: string): HighlightLike {
    let set = this.highlights.get(name);
    if (!set) {
      set = new Set();
      this.highlights.set(name, set);
    }
    return set;
  }
  adoptStyles(css: string): void {
    if (!this.sheets.includes(css)) this.sheets.push(css);
  }
  painted(step: number): string[] {
    return [...(this.highlights.get(`stream-edge-${step}`) ?? [])].map((r) => r.toString());
  }
  all(): string[] {
    return EDGE_FADE_STEP_OPACITY.flatMap((_, s) => this.painted(s));
  }
}

function mount(html: string): HTMLElement {
  document.body.innerHTML = `<div id="r">${html}</div>`;
  return document.getElementById("r")!;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] });
});
afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("edge fade", () => {
  it("paints the last graphemes in steps, newest faintest, across element boundaries", () => {
    const host = new FakeHost();
    paintEdgeFade(host, {}, mount("<p>hello <b>wonderful world</b> again</p>"));
    expect(host.painted(0)).toEqual(["gain"]);
    expect(host.all().join("").replace(/\s/g, "").length).toBe(EDGE_FADE_TAIL_GRAPHEMES);
  });

  it("counts a ZWJ emoji as one grapheme and never splits it", () => {
    const host = new FakeHost();
    paintEdgeFade(host, {}, mount("ab 👩🏽‍💻x"));
    expect(host.painted(0)).toEqual(["ab 👩🏽‍💻x"]);
  });

  it("repaints from scratch (no stale ranges) and clears on request", () => {
    const host = new FakeHost();
    const owner = {};
    const root = mount("abcdefgh");
    paintEdgeFade(host, owner, root);
    (root.firstChild as Text).data = "abcdefghijkl";
    paintEdgeFade(host, owner, root);
    expect(host.painted(0)).toEqual(["ijkl"]);
    clearEdgeFade(owner);
    expect(host.all()).toEqual([]);
  });

  it("two streaming items keep independent edges", () => {
    const host = new FakeHost();
    document.body.innerHTML = `<div id="a">aaaaaaaa</div><div id="b">bbbbbbbb</div>`;
    const a = {};
    paintEdgeFade(host, a, document.getElementById("a")!);
    paintEdgeFade(host, {}, document.getElementById("b")!);
    expect(host.painted(0).sort()).toEqual(["aaaa", "bbbb"]);
    clearEdgeFade(a);
    expect(host.painted(0)).toEqual(["bbbb"]);
  });

  it("settling ages the edge out over the settle window instead of snapping opaque", async () => {
    const host = new FakeHost();
    settleEdgeFade(host, {}, mount("abcdefghijklmnopqrstuvwxyz"));
    await vi.advanceTimersByTimeAsync(EDGE_FADE_SETTLE_MS / 2);
    const mid = host.all().join("").length;
    expect(mid).toBeGreaterThan(0);
    expect(mid).toBeLessThan(EDGE_FADE_TAIL_GRAPHEMES);
    await vi.advanceTimersByTimeAsync(EDGE_FADE_SETTLE_MS);
    expect(host.all()).toEqual([]);
  });

  it("cancelling a settle clears the edge at once", async () => {
    const host = new FakeHost();
    const cancel = settleEdgeFade(host, {}, mount("abcdefghijklmnop"));
    await vi.advanceTimersByTimeAsync(40);
    expect(host.all().length).toBeGreaterThan(0);
    cancel();
    expect(host.all()).toEqual([]);
  });

  it("asks the host for one stylesheet whose steps come from the constants", () => {
    const host = new FakeHost();
    const root = mount("abcdefgh");
    paintEdgeFade(host, {}, root);
    paintEdgeFade(host, {}, root);
    expect(host.sheets).toEqual([edgeFadeCss()]);
    for (let s = 0; s < EDGE_FADE_STEP_OPACITY.length; s++) {
      expect(edgeFadeCss()).toContain(
        `::highlight(stream-edge-${s}) { color: color-mix(in srgb, currentColor ${Math.round(EDGE_FADE_STEP_OPACITY[s] * 100)}%, transparent); }`,
      );
    }
  });
});
