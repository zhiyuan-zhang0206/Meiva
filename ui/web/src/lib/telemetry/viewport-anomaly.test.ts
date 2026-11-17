import { afterEach, expect, it } from "vitest";
import { fakeBrowser } from "../browser/testing";
import { initTelemetry, telemetryTestControl, type ViewportGeometry } from "./telemetry";
import { observeViewportAnomalies, type ViewportDiagnosticRuntime } from "./viewport-anomaly";

const cleanups: (() => void)[] = [];
afterEach(() => { for (const cleanup of cleanups.splice(0)) cleanup(); telemetryTestControl.reset(); });
function setup() {
  const tree = document.implementation.createHTMLDocument();
  tree.body.innerHTML = '<main data-viewport-shell><div data-viewport-composer></div></main>';
  const doc = Object.assign(new EventTarget(), {
    visibilityState: "visible",
    documentElement: tree.documentElement, body: tree.body,
    querySelector: (selector: string) => tree.querySelector(selector),
  }) as unknown as Document;
  const browser = fakeBrowser({ media: { "(pointer: coarse)": false } });
  initTelemetry(browser);
  let now = 0;
  let geometry = {
    layout_height: 1000, visual_height: 807, visual_scale: 1, html_inline_height: 807,
    html_height: 807, body_height: 807, shell_height: 807, shell_bottom: 807,
    composer_height: 50, composer_bottom: 807,
  };
  const frames = new Set<() => void>(); const deadlines = new Set<() => void>();
  const runtime: ViewportDiagnosticRuntime = {
    now: () => now, sample: () => ({ ...geometry }),
    frame: (work) => { frames.add(work); return () => { frames.delete(work); }; },
    deadline: (work) => { deadlines.add(work); return () => { deadlines.delete(work); }; },
  };
  const run = (works: Set<() => void>) => { const copy = [...works]; works.clear(); for (const work of copy) work(); };
  const mount = () => { const stop = observeViewportAnomalies(doc, browser, runtime); cleanups.push(stop); return stop; };
  const flush = () => {
    telemetryTestControl.flush();
    return browser.actions.beaconed.flatMap(({ body }) => (JSON.parse(body) as { events: { geometry: ViewportGeometry }[] }).events) as { geometry: ViewportGeometry }[];
  };
  return { doc, browser, runtime, mount, flush, frames, deadlines,
    frame: () => run(frames), expire: () => { now += 10_000; run(deadlines); }, advance: () => { now += 60_000; },
    change: (update: Partial<typeof geometry>) => { geometry = { ...geometry, ...update }; browser.answerViewport({ layoutHeight: geometry.layout_height, visual: null }); },
  };
}
it("sends an atomic pair through the buffer/beacon, on observed recovery", () => {
  const s = setup(); s.mount(); s.frame();
  expect(s.flush()[0].geometry).toMatchObject({ incident: 1, phase: "anomaly", html_inline_height: 807, composer_bottom: 807 });
  s.change({ shell_bottom: 1000, shell_height: 1000, visual_height: 1000 }); s.frame();
  const pair = s.flush();
  expect(pair.map((event) => event.geometry.phase)).toEqual(["anomaly", "recovered"]);
  expect(pair[1].geometry.shell_bottom).toBe(1000); expect(s.deadlines.size).toBe(0);
  expect(s.browser.actions.beaconed.map(({ body }) => (JSON.parse(body) as { session_id: string }).session_id)).toEqual([
    telemetryTestControl.sessionId(), telemetryTestControl.sessionId(),
  ]);
});
it("emits nothing for healthy geometry, zoom, coarse pointer or invalid numbers", () => {
  for (const mode of ["healthy", "zoom", "coarse", "invalid"]) {
    const s = setup();
    if (mode === "healthy") s.change({ shell_bottom: 1000 });
    if (mode === "zoom") s.change({ visual_scale: 1.25 });
    if (mode === "coarse") s.browser.answerMedia("(pointer: coarse)", true);
    if (mode === "invalid") s.change({ shell_height: Number.NaN });
    s.mount(); s.frame(); expect(s.flush()).toEqual([]);
  }
});
it("bounds document incidents across remounts, retaining cooldown and fixed sequence", () => {
  const s = setup();
  for (let incident = 1; incident <= 3; incident++) {
    const stop = s.mount(); s.frame(); s.expire(); stop();
    expect(s.flush().map((event) => event.geometry.incident)).toEqual(Array.from({ length: incident }, (_, i) => [i + 1, i + 1]).flat());
    const stopEarly = s.mount(); s.frame(); stopEarly(); expect(s.flush()).toHaveLength(incident * 2); s.advance();
  }
  s.mount(); s.frame(); s.expire(); expect(s.flush()).toHaveLength(6);
  expect(s.frames.size + s.deadlines.size).toBe(0); expect(s.browser.sizeWatches.every((watch) => watch.released)).toBe(true);
});
it("cancels pending frames, expiry and observers on unmount without refunding budget", () => {
  const s = setup(); const stop = s.mount(); stop(); expect(s.frames.size).toBe(0);
  const stopAgain = s.mount(); s.frame(); stopAgain(); expect(s.deadlines.size).toBe(0);
  expect(s.browser.sizeWatches.every((watch) => watch.released)).toBe(true);
  s.mount(); s.frame(); expect(s.flush()).toHaveLength(1);
});

it("suspends observers and all scheduled work when hidden, then resumes within budget", () => {
  const s = setup(); s.mount(); s.frame();
  Object.assign(s.doc, { visibilityState: "hidden" }); s.doc.dispatchEvent(new Event("visibilitychange"));
  expect(s.frames.size + s.deadlines.size).toBe(0);
  expect(s.browser.sizeWatches.every((watch) => watch.released)).toBe(true);
  s.advance(); Object.assign(s.doc, { visibilityState: "visible" });
  s.doc.dispatchEvent(new Event("visibilitychange")); s.frame(); s.expire();
  expect(s.flush().map(({ geometry }) => [geometry.incident, geometry.phase])).toEqual([
    [1, "anomaly"], [2, "anomaly"], [2, "timeout"],
  ]);
});
it("retains an unpinned short shell snapshot and treats missing samples as unavailable", () => {
  const s = setup(); s.change({ html_inline_height: 0, html_height: 1000, body_height: 1000 });
  s.mount(); s.frame(); expect(s.flush()[0].geometry).toMatchObject({ html_height: 1000, shell_height: 807 });
  s.runtime.sample = () => null; s.expire(); expect(s.flush()).toHaveLength(1);
});
it("does not report zoom as height recovery", () => {
  const s = setup(); s.mount(); s.frame(); s.change({ visual_scale: 1.25 }); s.frame();
  expect(s.flush()).toHaveLength(1); s.expire();
  expect(s.flush()[1].geometry.phase).toBe("timeout");
});
