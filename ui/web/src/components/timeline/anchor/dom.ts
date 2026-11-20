import { BAR_HEIGHT_PX } from "@/lib/layout/layout";

// meiva:note
// The reader's place is one value: the tail, or a row whose top edge sits `offset` px below the
// viewport's top edge (negative while a tall row is scrolled into). A row is a timeline item or a
// whole turn block; the two share the turn's first item id, so `turn` tells them apart.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export interface RowRef {
  itemId: string;
  rank: number;
  turn?: true;
}

export type ViewAnchor =
  | { kind: "tail" }
  | { kind: "row"; row: RowRef; offset: number };

export const TAIL: ViewAnchor = { kind: "tail" };

// meiva:note
// Where a row the reader is meant to start reading from sits: below the fixed header bar.
export const READING_TOP_PX = BAR_HEIGHT_PX + 12;

const esc = (value: string) => CSS.escape(value);

export function sameRow(a: RowRef, b: RowRef): boolean {
  return a.itemId === b.itemId && a.rank === b.rank && Boolean(a.turn) === Boolean(b.turn);
}

export function scrollViewportOf(wrapper: HTMLElement | null): HTMLElement | null {
  return wrapper?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
}

export interface ResolvedRow {
  node: HTMLElement;
  // meiva:note
  // The row is only reachable through its collapsed turn: the block stands in for it.
  collapsed: boolean;
}

// meiva:note
// Resolve a row in the mounted DOM: the exact row, else the collapsed turn holding it. A compact's
// re-rank is applied to the anchor before the rows change, so an id is never matched across ranks.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function findRowNode(viewport: HTMLElement, row: RowRef): ResolvedRow | null {
  const id = esc(row.itemId);
  if (row.turn) {
    // meiva:note
    // An older page can merge into the turn and give it a new first member: the item still names it.
    const turn = viewport.querySelector<HTMLElement>(`[data-turn-id][data-item-id="${id}"][data-display-rank="${row.rank}"]`)
      ?? viewport.querySelector<HTMLElement>(`[data-turn-id][data-turn-member-ranks~="${esc(`${row.rank}:${row.itemId}`)}"]`);
    return turn ? { node: turn, collapsed: false } : null;
  }
  const exact = viewport.querySelector<HTMLElement>(
    `.timeline-item[data-item-id="${id}"][data-display-rank="${row.rank}"], [data-turn-expanded="false"][data-item-id="${id}"][data-display-rank="${row.rank}"]`,
  );
  if (exact) return { node: exact, collapsed: exact.dataset.turnExpanded === "false" };
  const holder = viewport.querySelector<HTMLElement>(
    `[data-turn-expanded="false"][data-turn-member-ranks~="${esc(`${row.rank}:${row.itemId}`)}"]`,
  );
  return holder ? { node: holder, collapsed: true } : null;
}

export function offsetOf(viewport: HTMLElement, node: HTMLElement): number {
  return node.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
}

const READABLE_ROWS = ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]";

// meiva:note
// The reader's current place: the first row whose bottom clears the header bar. Pending and marker
// rows (`_` ids) never anchor, they are replaced in place by their committed rows.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function readRowAnchor(
  viewport: HTMLElement,
  // meiva:note
  // Rows that hold the head of the conversation (system prompt, standing notes) stay put while history
  // loads above the reader: they anchor only when nothing else is in view.
  pinnedHead: (id: string) => boolean = () => false,
): Extract<ViewAnchor, { kind: "row" }> | null {
  const box = viewport.getBoundingClientRect();
  let fallback: Extract<ViewAnchor, { kind: "row" }> | null = null;
  for (const node of viewport.querySelectorAll<HTMLElement>(READABLE_ROWS)) {
    const id = node.dataset.itemId;
    if (!id || id.startsWith("_")) continue;
    const rect = node.getBoundingClientRect();
    if (rect.bottom <= box.top + BAR_HEIGHT_PX || rect.top >= box.bottom) continue;
    const collapsedTurn = node.dataset.turnExpanded === "false";
    const anchor: Extract<ViewAnchor, { kind: "row" }> = {
      kind: "row",
      row: { itemId: id, rank: Number(node.dataset.displayRank ?? 0), ...(collapsedTurn ? { turn: true as const } : {}) },
      offset: rect.top - box.top,
    };
    if (!pinnedHead(id)) return anchor;
    fallback ??= anchor;
  }
  return fallback;
}

// meiva:note
// The identity the DOM gives a resolved node, when it differs from the row that found it.
export function rowOf(node: HTMLElement, row: RowRef, collapsed: boolean): RowRef {
  if (collapsed) return row;
  const itemId = node.dataset.itemId ?? row.itemId;
  const rank = Number(node.dataset.displayRank ?? row.rank);
  return itemId === row.itemId && rank === row.rank ? row : { itemId, rank, ...(row.turn ? { turn: true as const } : {}) };
}

// meiva:note
// A toggled turn keeps its header where the reader clicked it. An expanded header sticks below the
// bar while its body scrolls past, so the place is the button's, not the block's top edge.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/live-block.md
export function readTurnAnchor(viewport: HTMLElement, button: HTMLElement): Extract<ViewAnchor, { kind: "row" }> | null {
  const turn = button.closest<HTMLElement>("[data-turn-id][data-item-id]");
  const id = turn?.dataset.itemId;
  if (!turn || !id) return null;
  return { kind: "row", row: { itemId: id, rank: Number(turn.dataset.displayRank ?? 0), turn: true }, offset: offsetOf(viewport, button) };
}
