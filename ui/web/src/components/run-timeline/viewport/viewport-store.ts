import type { Viewport } from "../model/timeline-model-viewport";

// meiva:note
// The chart's viewport, held outside React: a wheel tick writes it, every canvas reads it in its next
// animation frame, and only the few labels that must follow it re-render. `get` returns the same object
// until the bounds move, which is what `useSyncExternalStore` needs; `set` is a no-op when they did not.
// A viewport change therefore costs one paint per row and no render of the chart's row tree (task #5738).
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

export interface ViewportStore {
  get(): Viewport;
  set(view: Viewport): void;
  subscribe(onChange: () => void): () => void;
}

export function viewportStore(initial: Viewport): ViewportStore {
  let view = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => view,
    set(next) {
      if (next.from === view.from && next.to === view.to) return;
      view = next;
      for (const listener of [...listeners]) listener();
    },
    subscribe(onChange) {
      listeners.add(onChange);
      return () => {
        listeners.delete(onChange);
      };
    },
  };
}
