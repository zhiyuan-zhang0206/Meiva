// meiva:note
// What a row's canvas would draw, as one string that changes exactly when one of its inputs does, so a
// render that changes nothing visible costs no paint; a test changes them one at a time.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

import { useSyncExternalStore } from "react";

import type { Selection } from "../model/timeline-model-selection";
import type { Highlight } from "../model/timeline-model-tree";

import type { RowDeco } from "./run-timeline-paint";

let nextLayoutVersion = 1;
const versions = new WeakMap<object, number>();

let themeEpoch = 0;
let watching = false;
const listeners = new Set<() => void>();

function watchTheme(): void {
  if (watching || typeof document === "undefined") return;
  watching = true;
  new MutationObserver(() => {
    themeEpoch += 1;
    for (const listener of listeners) listener();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
}

function subscribeTheme(onChange: () => void): () => void {
  watchTheme();
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

export function useThemeEpoch(): number {
  return useSyncExternalStore(subscribeTheme, () => themeEpoch, () => 0);
}

export function layoutVersion(layout: object): number {
  const known = versions.get(layout);
  if (known !== undefined) return known;
  const version = nextLayoutVersion++;
  versions.set(layout, version);
  return version;
}

function selectionId(selection: Selection | null): string {
  if (selection === null) return "-";
  return selection.kind === "node" ? `n${selection.id}` : `u${selection.unitKind}:${selection.i0}-${selection.i1}`;
}

function decoId(deco: RowDeco): string {
  const frame = (box: { left: number; width: number } | null) =>
    box === null ? "-" : `${box.left.toFixed(2)}+${box.width.toFixed(2)}`;
  return [
    deco.primaryKey ?? "-",
    [...deco.linkedKeys].sort().join(","),
    frame(deco.primary),
    frame(deco.linked),
    deco.lineX === null ? "-" : deco.lineX.toFixed(2),
  ].join("|");
}

function highlightId(highlight: Highlight | null): string {
  return highlight === null ? "-" : `${highlight.cls}:${highlight.source ?? ""}`;
}

export function rowPaintKey(parts: {
  layout: object;
  selection: Selection | null;
  hover: Selection | null;
  highlight: Highlight | null;
  deco: RowDeco;
  width: number;
  height: number;
  theme: number;
  extra?: string;
}): string {
  return [
    layoutVersion(parts.layout),
    selectionId(parts.selection),
    selectionId(parts.hover),
    highlightId(parts.highlight),
    decoId(parts.deco),
    `${parts.width}x${parts.height}`,
    `t${parts.theme}`,
    parts.extra ?? "-",
  ].join("/");
}

export function geometryKey(layouts: Iterable<object>): string {
  const versions: number[] = [];
  for (const layout of layouts) versions.push(layoutVersion(layout));
  return versions.join(",");
}
