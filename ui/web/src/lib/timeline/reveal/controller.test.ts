// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { describe, expect, it } from "vitest";

import {
  REVEAL_MAX_LAG_MS,
  REVEAL_MIN_GRAPHEMES_PER_SEC,
  REVEAL_SETTLE_MS,
  REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC,
} from "./constants";
import { smoothRevealSpeed, RevealController } from "./controller";
import {
  coefficientOfVariation,
  longestStallWithBacklogMs,
  maxLagMs,
  rateSeries,
  simulateReveal,
} from "./recordings/metrics";
import {
  REAL_TRACE_NAMES,
  SAMPLE_MARKDOWN,
  coalesceArrivals,
  realArrivals,
  synthesizeArrivals,
  type Arrival,
  type SyntheticProfile,
} from "./recordings/traces";

const FRAME = 1000 / 60;

function run(ctrl: RevealController, from: number, ms: number): number {
  let t = from;
  for (; t < from + ms; t += FRAME) ctrl.advance(t);
  return t;
}

describe("smoothRevealSpeed", () => {
  it("is the backlog divided by tau inside the [floor, ceiling] band", () => {
    expect(smoothRevealSpeed(30)).toBeCloseTo(100);
    expect(smoothRevealSpeed(60)).toBeCloseTo(200);
  });
  it("never drops below the floor (a tiny backlog keeps moving)", () => {
    expect(smoothRevealSpeed(1)).toBe(REVEAL_MIN_GRAPHEMES_PER_SEC);
    expect(smoothRevealSpeed(0.01)).toBe(REVEAL_MIN_GRAPHEMES_PER_SEC);
  });
  it("is capped at the soft ceiling for a burst (the lag bound lives in the controller's deadlines)", () => {
    expect(smoothRevealSpeed(400)).toBe(REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC);
    expect(smoothRevealSpeed(10_000)).toBe(REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC);
  });
});

describe("RevealController", () => {
  it("shows pre-existing text at once (history, refresh, remount) and is idle", () => {
    const ctrl = new RevealController("already here");
    expect(ctrl.shown()).toBe("already here");
    expect(ctrl.done).toBe(true);
  });

  it("reveals appended text gradually, never ahead of arrival, always a prefix", () => {
    const ctrl = new RevealController("");
    ctrl.update("hello wide world, this is a longer sentence", true, 0);
    ctrl.resume(0);
    expect(ctrl.shown()).toBe("");
    let t = 0;
    let prevLen = 0;
    for (let i = 0; i < 20; i++) {
      t += FRAME;
      ctrl.advance(t);
      const shown = ctrl.shown();
      expect("hello wide world, this is a longer sentence".startsWith(shown)).toBe(true);
      expect(shown.length).toBeGreaterThanOrEqual(prevLen);
      prevLen = shown.length;
    }
    expect(prevLen).toBeGreaterThan(0);
    expect(prevLen).toBeLessThan("hello wide world, this is a longer sentence".length);
  });

  it("uniform arrival: the display tracks the arrival rate with a bounded lag", () => {
    const arrivals: Arrival[] = Array.from({ length: 30 }, (_, i) => ({ tMs: i * 100, text: "abcdefghij" }));
    const samples = simulateReveal(arrivals);
    expect(maxLagMs(samples)).toBeLessThan(REVEAL_MAX_LAG_MS);
    const mid = samples.filter((s) => s.tMs >= 1000 && s.tMs < 2000);
    const rate = mid[mid.length - 1].shown - mid[0].shown;
    expect(rate).toBeGreaterThan(75);
    expect(rate).toBeLessThan(125);
  });

  it("burst: spreads a single 130-grapheme burst instead of showing it at once", () => {
    const arrivals: Arrival[] = [{ tMs: 0, text: "x".repeat(130) }];
    const samples = simulateReveal(arrivals, { commitDelayMs: 5000 });
    const at100 = samples.find((s) => s.tMs >= 100)!;
    expect(at100.shown).toBeGreaterThan(0);
    expect(at100.shown).toBeLessThan(80);
    const done = samples.find((s) => s.shown === 130)!;
    expect(done.tMs).toBeLessThan(REVEAL_MAX_LAG_MS);
    for (let i = 1; i < samples.length; i++) {
      const perFrame = samples[i].shown - samples[i - 1].shown;
      expect(perFrame).toBeLessThanOrEqual(Math.ceil((REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC * FRAME) / 1000) + 1);
    }
  });

  it("long pause: the display drains the backlog and then waits without drifting ahead", () => {
    const ctrl = new RevealController("");
    ctrl.update("abc", true, 0);
    ctrl.resume(0);
    let t = run(ctrl, 0, 500);
    expect(ctrl.shown()).toBe("abc");
    expect(ctrl.done).toBe(true);
    t = run(ctrl, t, 2000);
    expect(ctrl.shown()).toBe("abc");
    ctrl.update("abcdef", true, t);
    ctrl.resume(t);
    ctrl.advance(t + FRAME);
    expect(ctrl.shown().length).toBeLessThanOrEqual(4);
  });

  it("a frame after a long stall shows what is already overdue instead of crawling", () => {
    const ctrl = new RevealController("");
    ctrl.update("x".repeat(200), true, 0);
    ctrl.resume(0);
    ctrl.advance(60_000);
    expect(ctrl.done).toBe(true);
  });

  it("huge backlog catches up so the lag stays bounded", () => {
    const arrivals: Arrival[] = [{ tMs: 0, text: "y".repeat(6000) }];
    const samples = simulateReveal(arrivals, { commitDelayMs: 10_000 });
    const done = samples.find((s) => s.shown === 6000)!;
    expect(done.tMs).toBeLessThanOrEqual(REVEAL_MAX_LAG_MS + 2 * FRAME);
  });

  it("settle: after commit the remaining backlog flushes within the settle window", () => {
    const ctrl = new RevealController("");
    ctrl.update("z".repeat(300), true, 0);
    ctrl.resume(0);
    const t = run(ctrl, 0, 100);
    expect(ctrl.done).toBe(false);
    ctrl.update("z".repeat(300), false, t);
    const end = run(ctrl, t, REVEAL_SETTLE_MS + 2 * FRAME);
    expect(ctrl.shown()).toBe("z".repeat(300));
    expect(end - t).toBeLessThanOrEqual(REVEAL_SETTLE_MS + 3 * FRAME);
  });

  it("settle with an unrelated backlog size still ends within the window", () => {
    for (const n of [5, 80, 2000]) {
      const ctrl = new RevealController("");
      ctrl.update("w".repeat(n), true, 0);
      ctrl.resume(0);
      ctrl.update("w".repeat(n), false, 0);
      run(ctrl, 0, REVEAL_SETTLE_MS + 2 * FRAME);
      expect(ctrl.done).toBe(true);
    }
  });

  it("a commit whose text extends the shown text continues without resetting the reveal", () => {
    const ctrl = new RevealController("");
    ctrl.update("The quick brown fox jumps over", true, 0);
    ctrl.resume(0);
    const t = run(ctrl, 0, 120);
    const before = ctrl.shown();
    expect(before.length).toBeGreaterThan(0);
    ctrl.update("The quick brown fox jumps over the lazy dog.", false, t);
    expect(ctrl.shown()).toBe(before);
    ctrl.advance(t + FRAME);
    expect(ctrl.shown().startsWith(before)).toBe(true);
  });

  it("a commit that rewrites the text keeps the still-matching prefix and re-reveals the rest", () => {
    const ctrl = new RevealController("");
    ctrl.update("Hello wor", true, 0);
    ctrl.resume(0);
    const t = run(ctrl, 0, 400);
    expect(ctrl.shown()).toBe("Hello wor");
    ctrl.update("Hello, world!", false, t);
    expect(ctrl.shown()).toBe("Hello");
    run(ctrl, t, REVEAL_SETTLE_MS + 2 * FRAME);
    expect(ctrl.shown()).toBe("Hello, world!");
  });

  it("a committed item whose text is replaced is shown at once (not a stream)", () => {
    const ctrl = new RevealController("old committed text", false);
    ctrl.update("completely different text", false, 0);
    expect(ctrl.shown()).toBe("completely different text");
    expect(ctrl.done).toBe(true);
  });

  it("snap() shows everything that has arrived (tab visible again)", () => {
    const ctrl = new RevealController("");
    ctrl.update("q".repeat(500), true, 0);
    ctrl.snap();
    expect(ctrl.shown()).toBe("q".repeat(500));
    expect(ctrl.done).toBe(true);
  });
});

describe("RevealController: Unicode", () => {
  const family = "👨‍👩‍👧‍👦";
  const flag = "🇨🇳";
  const accented = "é";
  const text = `a${family}b${flag}${accented}\u4e2d\u6587c`;
  const graphemes = [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)].map((s) => s.segment);

  it("never cuts a grapheme: every shown prefix ends on a grapheme boundary", () => {
    const boundaries = new Set<number>([0]);
    let off = 0;
    for (const g of graphemes) boundaries.add((off += g.length));
    const ctrl = new RevealController("");
    ctrl.update(text, true, 0);
    ctrl.resume(0);
    for (let t = 0; t < 1500; t += FRAME) {
      ctrl.advance(t);
      expect(boundaries.has(ctrl.shownEnd())).toBe(true);
    }
    expect(ctrl.shown()).toBe(text);
  });

  it("re-segments the seam when a combining mark / ZWJ part arrives in a later chunk", () => {
    const ctrl = new RevealController("");
    ctrl.update("e", true, 0);
    ctrl.update("éx", true, 1);
    ctrl.snap();
    expect(ctrl.shownCount()).toBe(2);
    const c2 = new RevealController("");
    c2.update("👨", true, 0);
    c2.update("👨‍👩", true, 1);
    c2.snap();
    expect(c2.shownCount()).toBe(1);
  });

  it("holds back a trailing lone surrogate until its pair arrives", () => {
    const [hi, lo] = ["\ud83d", "\ude00"];
    const ctrl = new RevealController("");
    ctrl.update(`ok${hi}`, true, 0);
    ctrl.snap();
    expect(ctrl.shown()).toBe("ok");
    ctrl.update(`ok${hi}${lo}`, true, 1);
    ctrl.snap();
    expect(ctrl.shown()).toBe("ok😀");
  });

  it("a committed stream shows even a genuinely broken trailing surrogate", () => {
    const ctrl = new RevealController("");
    ctrl.update("ok\ud83d", false, 0);
    ctrl.snap();
    expect(ctrl.shown()).toBe("ok\ud83d");
  });
});

describe("smoothing quality on real arrival traces", () => {
  it.each(REAL_TRACE_NAMES)("%s: ends fully shown, never ahead of arrival, lag bounded", (name) => {
    const coalesced = coalesceArrivals(realArrivals(name), 40);
    const samples = simulateReveal(coalesced);
    const last = samples[samples.length - 1];
    expect(last.shown).toBe(last.arrived);
    for (const s of samples) expect(s.shown).toBeLessThanOrEqual(s.arrived);
    expect(maxLagMs(samples)).toBeLessThanOrEqual(REVEAL_MAX_LAG_MS);
  });

  it("real Claude thinking burst (126 graphemes in 5ms): display peak rate is a fraction of the arrival spike", () => {
    const arrivals = coalesceArrivals(realArrivals("claude_thinking_text_tool"), 40);
    const samples = simulateReveal(arrivals);
    const arrivalPeak = Math.max(...rateSeries(samples, 50, 1000, (s) => s.arrived));
    const displayPeak = Math.max(...rateSeries(samples, 50, 1000, (s) => s.shown));
    expect(displayPeak).toBeLessThan(arrivalPeak / 3);
  });

  const SYNTHETIC: [SyntheticProfile, number][] = [
    ["claude-burst", 0.6],
    ["codex-packets", 0.6],
    ["antigravity-sentences", 0.6],
    ["steady-then-stall", 0.85],
  ];
  const long = SAMPLE_MARKDOWN.repeat(4);
  it.each(SYNTHETIC)("SYNTHETIC %s: display speed varies much less than arrival speed, lag bounded", (profile, ratio) => {
    const arrivals = coalesceArrivals(synthesizeArrivals(profile, long, 7), 40);
    const samples = simulateReveal(arrivals);
    const span = samples[samples.length - 1].tMs;
    const arrivalCv = coefficientOfVariation(rateSeries(samples, 100, span, (s) => s.arrived));
    const displayCv = coefficientOfVariation(rateSeries(samples, 100, span, (s) => s.shown));
    expect(displayCv).toBeLessThan(arrivalCv * ratio);
    expect(maxLagMs(samples)).toBeLessThanOrEqual(REVEAL_MAX_LAG_MS + 2 * FRAME);
    const last = samples[samples.length - 1];
    expect(last.shown).toBe(last.arrived);
  });

  it("SYNTHETIC codex-packets: the display never freezes while a backlog exists", () => {
    const samples = simulateReveal(coalesceArrivals(synthesizeArrivals("codex-packets", long, 3), 40));
    expect(longestStallWithBacklogMs(samples)).toBeLessThanOrEqual(100);
  });
});
