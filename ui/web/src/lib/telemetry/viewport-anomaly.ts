import type { Browser } from "@/lib/browser/browser";
import { track, type ViewportGeometry } from "./telemetry";

type Reason = ViewportGeometry["reason"];
type Sample = Omit<ViewportGeometry, "incident" | "phase" | "elapsed_ms" | "reason">;
interface Budget { count: number; last: number; active: boolean }
// meiva:note
// The owning document, rather than the component mount, owns the finite budget.
// Hiding or routing away cancels an unfinished pair without refunding its incident.
const budgets = new WeakMap<Document, Budget>();

export interface ViewportDiagnosticRuntime {
  now(): number;
  frame(work: () => void): () => void;
  deadline(work: () => void): () => void;
  sample(): Sample | null;
}

export function viewportDiagnosticOver(doc: Document, browser: Browser): ViewportDiagnosticRuntime {
  const view = doc.defaultView;
  if (view === null) throw new Error("viewport diagnostic requires a document view");
  return {
    now: () => view.performance.now(),
    frame(work) {
      const id = view.requestAnimationFrame(work);
      return () => view.cancelAnimationFrame(id);
    },
    deadline(work) {
      const id = view.setTimeout(work, 10_000);
      return () => view.clearTimeout(id);
    },
    sample() {
      const shell = doc.querySelector("[data-viewport-shell]");
      if (shell === null) return null;
      const reading = browser.viewport.read();
      const composer = doc.querySelector("[data-viewport-composer]")?.getBoundingClientRect();
      const rect = shell.getBoundingClientRect();
      const inline = doc.documentElement.style.height;
      return {
        layout_height: reading.layoutHeight,
        visual_height: reading.visual?.height ?? null,
        visual_scale: reading.visual?.scale ?? null,
        html_inline_height: /^\d+(?:\.\d+)?px$/.test(inline) ? Number.parseFloat(inline) : null,
        html_height: doc.documentElement.getBoundingClientRect().height,
        body_height: doc.body.getBoundingClientRect().height,
        shell_height: rect.height,
        shell_bottom: rect.bottom,
        composer_height: composer?.height ?? null,
        composer_bottom: composer?.bottom ?? null,
      };
    },
  };
}

function valid(sample: Sample): boolean {
  return Object.entries(sample).every(([key, value]) => value === null || (
    Number.isFinite(value) && value <= (key === "visual_scale" ? 100 : 100_000) &&
    value >= (key.endsWith("bottom") ? -100_000 : key === "visual_scale" ? 0.01 : 0)
  ));
}

function short(sample: Sample): boolean {
  return sample.layout_height > 0 && sample.visual_scale !== null &&
    Math.abs(sample.visual_scale - 1) < 0.01 && sample.layout_height - sample.shell_bottom > 150;
}

export function observeViewportAnomalies(
  doc: Document, browser: Browser, runtime = viewportDiagnosticOver(doc, browser),
): () => void {
  let budget = budgets.get(doc);
  if (budget === undefined) {
    budget = { count: 0, last: -Infinity, active: false };
    budgets.set(doc, budget);
  }
  if (budget.active || budget.count >= 3) return () => undefined;
  budget.active = true;
  const state = budget;
  const coarse = browser.media.query("(pointer: coarse)");
  let cancelFrame: (() => void) | null = null;
  let cancelDeadline: (() => void) | null = null;
  let pending: { incident: 1 | 2 | 3; started: number } | null = null;
  let subscriptions: (() => void)[] = [];
  let disposed = false;

  const emit = (sample: Sample, phase: ViewportGeometry["phase"], reason: Reason) => {
    if (pending === null) return;
    track("viewport-anomaly", {
      page: "home", dedupe: false,
      geometry: { ...sample, incident: pending.incident, phase, reason,
        elapsed_ms: Math.min(86_400_000, Math.max(0, runtime.now() - pending.started)) },
    });
  };
  const finish = (sample: Sample, phase: "recovered" | "timeout", reason: Reason) => {
    emit(sample, phase, reason);
    pending = null;
    cancelDeadline?.();
    cancelDeadline = null;
    if (state.count >= 3) stopWatching();
  };
  const sample = (reason: Reason) => {
    if (disposed || doc.visibilityState === "hidden") return;
    const geometry = runtime.sample();
    if (geometry === null || !valid(geometry)) return;
    if (pending !== null) {
      if (geometry.layout_height > 0 && geometry.layout_height - geometry.shell_bottom <= 150) finish(geometry, "recovered", reason);
      return;
    }
    // meiva:note
    // This desktop-shaped filter limits diagnostic noise; it does not classify
    // keyboards. A geometry-only compositor failure remains outside this signal.
    if (coarse.matches() || !short(geometry) || state.count >= 3 || runtime.now() - state.last < 60_000) return;
    const incident = state.count === 0 ? 1 : state.count === 1 ? 2 : 3;
    state.count = incident;
    state.last = runtime.now();
    pending = { incident, started: state.last };
    emit(geometry, "anomaly", reason);
    cancelDeadline = runtime.deadline(() => {
      cancelDeadline = null;
      const latest = runtime.sample();
      if (latest !== null && valid(latest)) finish(latest, latest.layout_height > 0 && latest.layout_height - latest.shell_bottom <= 150 ? "recovered" : "timeout", "deadline");
      else { pending = null; if (state.count >= 3) stopWatching(); }
    });
  };
  const schedule = (reason: Reason) => {
    if (cancelFrame !== null || disposed || doc.visibilityState === "hidden") return;
    cancelFrame = runtime.frame(() => { cancelFrame = null; sample(reason); });
  };
  function stopWatching() {
    for (const unsubscribe of subscriptions) unsubscribe();
    subscriptions = [];
    cancelFrame?.();
    cancelFrame = null;
    cancelDeadline?.();
    cancelDeadline = null;
    pending = null;
  }
  const startWatching = (reason: Reason) => {
    if (disposed || doc.visibilityState === "hidden" || state.count >= 3) return;
    subscriptions.push(browser.viewport.subscribe(() => schedule("viewport-change")));
    for (const target of [doc.documentElement, doc.body,
      doc.querySelector("[data-viewport-shell]"), doc.querySelector("[data-viewport-composer]")]) {
      if (target !== null) subscriptions.push(browser.size.observe(target, () => schedule("box-change")));
    }
    schedule(reason);
  };
  const visibility = () => {
    stopWatching();
    if (doc.visibilityState !== "hidden") startWatching("visible");
  };
  doc.addEventListener("visibilitychange", visibility);
  doc.defaultView?.addEventListener("pagehide", stopWatching);
  startWatching("mount");
  return () => {
    disposed = true;
    stopWatching();
    doc.removeEventListener("visibilitychange", visibility);
    doc.defaultView?.removeEventListener("pagehide", stopWatching);
    state.active = false;
  };
}
