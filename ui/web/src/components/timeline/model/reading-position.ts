import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { saveScrollMemory } from "@/lib/layout/scroll-memory";
import type { DetailsMode } from "@/lib/layout/content-toggle-store";
import { isAtBottom, type StickyThresholds } from "@/lib/layout/sticky";

// meiva:note
// A saved position is one of two kinds. A reader inside the bottom zone is watching the tail, not a row, so the bookmark says "follow the bottom" and a return lands on the newest message however much arrived meanwhile; a reader above the zone keeps the visible item and its offset and is returned to that item. Bottom-ness is the same measure the scroll-to-bottom button and the follow rule use, so the saved kind matches what the reader was shown.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
export function rememberTimelinePosition(
  viewport: HTMLElement, entryKey: string, contentKey: string, thresholds: StickyThresholds,
) {
  const position = {
    scrollTop: viewport.scrollTop,
    scrollHeight: viewport.scrollHeight,
    clientHeight: viewport.clientHeight,
  };
  if (isAtBottom(position, thresholds)) {
    saveScrollMemory(entryKey, { contentKey, scrollTop: position.scrollTop, followBottom: true });
    return;
  }
  const rect = viewport.getBoundingClientRect();
  const node = [...viewport.querySelectorAll<HTMLElement>(
    ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
  )].find((item) => {
    const box = item.getBoundingClientRect();
    return box.bottom > rect.top + BAR_HEIGHT_PX && box.top < rect.bottom;
  });
  saveScrollMemory(entryKey, {
    contentKey, scrollTop: viewport.scrollTop, followBottom: false,
    anchor: node?.dataset.itemId ? {
      itemId: node.dataset.itemId, rank: Number(node.dataset.displayRank ?? 0),
      viewportTop: node.getBoundingClientRect().top - rect.top,
    } : undefined,
  });
}

interface ReadingExpansion {
  mode: DetailsMode;
  items: ReadonlySet<string>;
  turns: ReadonlyMap<string, boolean>;
}
// Only ephemeral view choices live here. Server rows stay in the QueryClient.
const expansions = new Map<string, ReadingExpansion>();
export function readTimelineExpansion(key: string | undefined, mode: DetailsMode) {
  const saved = key ? expansions.get(key) : undefined;
  return saved?.mode === mode ? saved : undefined;
}
export function rememberTimelineExpansion(key: string | undefined, value: ReadingExpansion) {
  if (key) expansions.set(key, value);
}


/** Scroll keys and scrollbar drags resume normal following after a bookmark. */
export function watchReadingNavigation(viewport: HTMLElement, wrapper: HTMLElement | null, resume: () => void) {
  const keyDown = (event: KeyboardEvent) => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) resume();
  };
  const pointerDown = (event: PointerEvent) => {
    if ((event.target as HTMLElement).closest('[data-slot="scroll-area-scrollbar"]')) resume();
  };
  viewport.addEventListener("keydown", keyDown);
  wrapper?.addEventListener("pointerdown", pointerDown, { capture: true });
  return () => {
    viewport.removeEventListener("keydown", keyDown);
    wrapper?.removeEventListener("pointerdown", pointerDown, { capture: true });
  };
}
