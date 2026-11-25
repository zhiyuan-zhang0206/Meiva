// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md
import {
  EDGE_FADE_AGE_OUT_MS,
  EDGE_FADE_STEP_OPACITY,
  EDGE_FADE_TAIL_GRAPHEMES,
} from "./constants";

export interface HighlightLike {
  add(range: Range): unknown;
  delete(range: Range): unknown;
}

export interface HighlightHost {
  highlight(name: string): HighlightLike;
  adoptStyles(css: string): void;
}

const STEPS = EDGE_FADE_STEP_OPACITY.length;
const GRAPHEMES_PER_STEP = EDGE_FADE_TAIL_GRAPHEMES / STEPS;
const highlightName = (step: number) => `stream-edge-${step}`;

// Inside `::highlight()` `currentColor` does not resolve to the host text's colour: Chromium
// uses the highlight's own initial value (opaque black), which blends to a near-background
// dark in dark mode. Custom properties do inherit from the host element, so each text surface
// declares `--stream-edge-color` as its real foreground (see stream-reveal.md).
export function edgeFadeCss(): string {
  return EDGE_FADE_STEP_OPACITY.map(
    (opacity, step) =>
      `::highlight(${highlightName(step)}) { color: color-mix(in srgb, var(--stream-edge-color, var(--foreground)) ${Math.round(opacity * 100)}%, transparent); }`,
  ).join("\n");
}

let domHost: HighlightHost | null | undefined;

export function domHighlightHost(): HighlightHost | null {
  if (domHost !== undefined) return domHost;
  if (typeof CSS === "undefined" || !("highlights" in CSS) || typeof Highlight === "undefined") {
    domHost = null;
    return domHost;
  }
  const adopted = new Set<string>();
  domHost = {
    highlight(name) {
      let h = CSS.highlights.get(name);
      if (!h) {
        h = new Highlight();
        CSS.highlights.set(name, h);
      }
      return h;
    },
    adoptStyles(css) {
      if (adopted.has(css)) return;
      adopted.add(css);
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(css);
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    },
  };
  return domHost;
}

interface Painted {
  host: HighlightHost;
  ranges: Map<number, Range[]>;
}
const paintedByOwner = new WeakMap<object, Painted>();
let segmenter: Intl.Segmenter | null = null;

export function clearEdgeFade(owner: object): void {
  const prev = paintedByOwner.get(owner);
  if (!prev) return;
  for (const [step, ranges] of prev.ranges) {
    const highlight = prev.host.highlight(highlightName(step));
    for (const r of ranges) highlight.delete(r);
  }
  paintedByOwner.delete(owner);
}

function stepOf(distance: number): number {
  return Math.floor(distance / GRAPHEMES_PER_STEP);
}

export function paintEdgeFade(host: HighlightHost, owner: object, root: HTMLElement, age = 0): void {
  host.adoptStyles(edgeFadeCss());
  clearEdgeFade(owner);
  segmenter ??= new Intl.Segmenter(undefined, { granularity: "grapheme" });
  const next = new Map<number, Range[]>();
  let last: Node = root;
  while (last.lastChild) last = last.lastChild;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  walker.currentNode = last;
  let node: Node | null = last instanceof Text ? last : walker.previousNode();
  let distance = age;
  while (node instanceof Text && stepOf(distance) < STEPS) {
    const textNode = node;
    const from = Math.max(0, textNode.data.length - 4 * EDGE_FADE_TAIL_GRAPHEMES);
    const pieces = [...segmenter.segment(textNode.data.slice(from))];
    const runs: { step: number; start: number; end: number }[] = [];
    for (let p = pieces.length - 1; p >= 0; p--) {
      const step = stepOf(distance);
      if (step >= STEPS) break;
      const { index, segment } = pieces[p];
      const current = runs.at(-1);
      if (current?.step === step) current.start = index;
      else runs.push({ step, start: index, end: index + segment.length });
      if (segment.trim() !== "") distance++;
    }
    for (const run of runs) {
      const range = document.createRange();
      range.setStart(textNode, from + run.start);
      range.setEnd(textNode, from + run.end);
      next.set(run.step, [...(next.get(run.step) ?? []), range]);
    }
    node = walker.previousNode();
  }
  for (const [step, ranges] of next) {
    const highlight = host.highlight(highlightName(step));
    for (const r of ranges) highlight.add(r);
  }
  paintedByOwner.set(owner, { host, ranges: next });
}

export function settleEdgeFade(host: HighlightHost, owner: object, root: HTMLElement): () => void {
  let frame = 0;
  let cancelled = false;
  const startedAt = performance.now();
  const step = (ts: number) => {
    if (cancelled) return;
    const progress = Math.min(1, (ts - startedAt) / EDGE_FADE_AGE_OUT_MS);
    if (progress >= 1) {
      clearEdgeFade(owner);
      return;
    }
    paintEdgeFade(host, owner, root, Math.round(progress * EDGE_FADE_TAIL_GRAPHEMES));
    frame = requestAnimationFrame(step);
  };
  frame = requestAnimationFrame(step);
  return () => {
    cancelled = true;
    cancelAnimationFrame(frame);
    clearEdgeFade(owner);
  };
}
