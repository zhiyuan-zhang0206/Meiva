import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { saveScrollMemory } from "@/lib/layout/scroll-memory";

// meiva:note
// Leaving a conversation always saves the visible item and offset, including
// at the tail or between a stream chunk and its pin. Returning continues that
// reading position while newer output remains below it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function rememberTimelinePosition(
  viewport: HTMLElement, entryKey: string, contentKey: string,
  collapseReserve = 0, keepSendReserve = false,
) {
  const reserveMemory = keepSendReserve ? { keepSendReserve: true as const } : {};
  const rect = viewport.getBoundingClientRect();
  const node = [...viewport.querySelectorAll<HTMLElement>(
    ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
  )].find((item) => {
    const box = item.getBoundingClientRect();
    return box.bottom > rect.top + BAR_HEIGHT_PX && box.top < rect.bottom;
  });
  saveScrollMemory(entryKey, {
    contentKey, scrollTop: viewport.scrollTop, followBottom: false, ...reserveMemory,
    ...(collapseReserve > 0 && viewport.scrollTop + viewport.clientHeight > viewport.scrollHeight - collapseReserve + 1
      ? { usedCollapseReserve: true as const } : {}),
    anchor: node?.dataset.itemId ? {
      itemId: node.dataset.itemId, rank: Number(node.dataset.displayRank ?? 0),
      viewportTop: node.getBoundingClientRect().top - rect.top,
    } : undefined,
  });
}

interface ReadingExpansion {
  items: ReadonlyMap<string, boolean>;
  turns: ReadonlyMap<string, boolean>;
}
// Only ephemeral view choices live here. Server rows stay in the QueryClient.
const expansions = new Map<string, ReadingExpansion>();
export function readTimelineExpansion(key: string | undefined) {
  return key ? expansions.get(key) : undefined;
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
    const el = event.target as HTMLElement | null;
    if (el?.closest('[data-slot="scroll-area-scrollbar"]') && !el.closest('[data-activity-scroll="true"]')) {
      resume();
    }
  };
  viewport.addEventListener("keydown", keyDown);
  wrapper?.addEventListener("pointerdown", pointerDown, { capture: true });
  return () => {
    viewport.removeEventListener("keydown", keyDown);
    wrapper?.removeEventListener("pointerdown", pointerDown, { capture: true });
  };
}
