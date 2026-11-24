import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { findOldestTimelineCursor } from "@/lib/timeline/timeline";

import type { ReloadRestoreConfig } from "../model/reading-position";
import { sameRow, TAIL, type RowRef, type ViewAnchor } from "./dom";
import { locate } from "./rows";
import { readSessionPlace, rowAnchor, type SavedPlace, type TailDeparture } from "./saved";
import { unreadBoundary } from "./unread";

// meiva:note
// Where a mounted conversation opens, and when its rows may be seen. The goal is the saved place
// (this page's memory, else the tab's reload bookmark), else the first unseen delivery, else the
// tail. Rows stay hidden under the one loading ring until the goal resolves in the loaded rows, so
// the first painted frame is the destination:
// - retained rows that hold the saved row open on it at once;
// - a missing row is searched for in a bounded number of older pages;
// - an unreachable row opens the tail.
// A placed row that later leaves the loaded rows is resolved again the same way. A pure machine:
// each layout pass feeds it the inputs, and it answers with the effects to perform.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

export interface RevealDecision {
  decided: boolean;
  itemId: string | null;
  entrySeenId?: number;
  olderPages?: number;
}

export interface SearchBounds {
  maxPages: number;
  maxItems: number;
  timeoutMs: number;
}

export interface PlacementInputs {
  threadKey: string | undefined;
  reloadRestore: ReloadRestoreConfig;
  items: readonly BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  loading: boolean;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  canLoadOlder: boolean;
  reveal: RevealDecision;
  now: number;
  // meiva:note
  // A first page shorter than the viewport is still reading its older pages: opening now would
  // reflow the rows under the reader as they land.
  filling?: () => boolean;
}

export interface PlacementEffects {
  place: (anchor: ViewAnchor, options?: { smooth?: boolean }) => void;
  loadOlder: () => void;
  cancelOlder: () => void;
  // meiva:note
  // A reload bookmark that can never land is dropped, so the next reload opens on the tail.
  dropSessionPlace: () => void;
}

interface Search {
  pages: number;
  requested: string;
  // meiva:note
  // The oldest row when the search began: only rows prepended before it count against the bound,
  // not rows streaming in at the tail meanwhile.
  baseline: string;
  sawLoading: boolean;
}

interface SearchGoal {
  search?: Search;
  waitingSince?: number;
}

type Goal =
  | { kind: "settings" }
  | ({ kind: "first"; departure?: TailDeparture } & SearchGoal)
  | { kind: "tail" }
  | ({ kind: "row"; place: SavedPlace; fromSession: boolean; bounded?: boolean } & SearchGoal)
  | { kind: "placed" };

export interface PlacementSnapshot {
  visible: boolean;
  searching: boolean;
  pin: RowRef | null;
  // meiva:note
  // The cold fill may read older pages: the rows are placed, or a first visit waits for them.
  fillAllowed: boolean;
}

export function initialGoal(memory: SavedPlace | TailDeparture | null, threadKey: string | undefined, reloadRestore: ReloadRestoreConfig): Goal {
  if (memory && "last" in memory) return { kind: "first", departure: memory };
  if (memory) return { kind: "row", place: memory, fromSession: false };
  if (threadKey === undefined) return { kind: "tail" };
  if (reloadRestore.state === "loading") return { kind: "settings" };
  const session = readSessionPlace(threadKey, reloadRestore);
  return session ? { kind: "row", place: session, fromSession: true } : { kind: "first" };
}

export class PlacementMachine {
  private goal: Goal;
  private requested: { anchor: ViewAnchor; smooth?: boolean } | null = null;
  private placedRow: RowRef | null = null;
  private snap: PlacementSnapshot;
  private readonly listeners = new Set<() => void>();

  constructor(goal: Goal, private readonly bounds: SearchBounds) {
    this.goal = goal;
    this.snap = { visible: goal.kind === "placed", searching: false, pin: null, fillAllowed: goal.kind === "placed" || goal.kind === "first" };
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  snapshot = () => this.snap;

  // meiva:note
  // The row a placement in flight needs mounted, given the loaded rows.
  goalRow(inputs: Pick<PlacementInputs, "items" | "compactBuffer" | "reveal">): RowRef | null {
    const goal = this.goal;
    if (goal.kind === "row") return locate(goal.place.identity, inputs.items, inputs.compactBuffer);
    if (goal.kind === "first" && inputs.reveal.decided) {
      return unreadBoundary(inputs.items, inputs.reveal, goal.departure, inputs.compactBuffer).row;
    }
    return null;
  }

  // meiva:note
  // Advance with this layout pass's inputs, performing the placements it reaches.
  step(inputs: PlacementInputs, effects: PlacementEffects): void {
    for (let guard = 0; guard < 4 && this.advance(inputs, effects); guard++);
    if (this.goal.kind === "placed" && this.requested) {
      const { anchor, smooth } = this.requested;
      this.requested = null;
      this.placedRow = anchor.kind === "tail" ? null : anchor.row;
      effects.place(anchor, { smooth });
    }
    this.publish(inputs);
  }

  // meiva:note
  // A placement the reader asked for (quote, send, the arrow): lands once its row is mounted.
  request(anchor: ViewAnchor, inputs: PlacementInputs, { smooth = false }: { smooth?: boolean } = {}): void {
    this.requested = { anchor, smooth };
    this.publish(inputs);
  }

  // meiva:note
  // The reader moved: the window keeps their row mounted (a prepend or a re-estimated spacer must not
  // unmount it), or nothing while they follow the tail or stand in unmounted rows.
  readerMoved(inputs: PlacementInputs, row: RowRef | null): void {
    const same = row === null ? this.placedRow === null : this.placedRow !== null && sameRow(this.placedRow, row);
    if (same) return;
    this.placedRow = row;
    this.publish(inputs);
  }

  // meiva:note
  // A placed row left the loaded rows: resolve it again (hidden) or move to where it went.
  lost(place: SavedPlace, inputs: PlacementInputs, effects: PlacementEffects, bounded = false): void {
    if (this.goal.kind !== "placed") return;
    const moved = locate(place.identity, inputs.items, inputs.compactBuffer);
    if (moved) {
      if (!sameRow(moved, place.identity.row)) effects.place(bounded ? { kind: "bounded-tail", row: moved } : rowAnchor(place, moved));
      return;
    }
    this.goal = { kind: "row", place, fromSession: false, bounded };
    this.step(inputs, effects);
  }

  isPlaced(): boolean {
    return this.goal.kind === "placed";
  }

  private settle(target: ViewAnchor, effects: PlacementEffects): void {
    this.goal = { kind: "placed" };
    this.placedRow = target.kind === "tail" ? null : target.row;
    effects.place(target);
  }

  // meiva:note
  // One transition; true when another may follow in the same pass.
  private advance(inputs: PlacementInputs, effects: PlacementEffects): boolean {
    const goal = this.goal;
    const { items, loading } = inputs;
    switch (goal.kind) {
      case "placed":
        return false;
      case "settings": {
        if (inputs.reloadRestore.state === "loading") return false;
        this.goal = initialGoal(null, inputs.threadKey, inputs.reloadRestore);
        return true;
      }
      case "first": {
        if (loading && (goal.departure || items.length === 0)) return false;
        const boundary = inputs.reveal.decided
          ? unreadBoundary(items, inputs.reveal, goal.departure, inputs.compactBuffer)
          : null;
        const finish = () => {
          const row = boundary?.row;
          this.settle(row ? { kind: "bounded-tail", row } : TAIL, effects);
          return false;
        };
        if (boundary?.needsOlder && (inputs.hasMoreOlder || goal.search)) {
          return this.searchOlder(goal, inputs, effects, finish);
        }
        if (!boundary || inputs.filling?.() === true) {
          // meiva:note
          // The rows are in but the reveal decision or the fill is not: wait, within the same time bound.
          if (goal.waitingSince === undefined) {
            this.goal = { ...goal, waitingSince: inputs.now };
            return false;
          }
          if (inputs.now - goal.waitingSince < this.bounds.timeoutMs) return false;
        }
        return finish();
      }
      case "tail": {
        if (loading && items.length === 0) return false;
        this.settle(TAIL, effects);
        return false;
      }
      case "row": {
        const row = this.goalRow(inputs);
        if (row) {
          this.settle(goal.bounded ? { kind: "bounded-tail", row } : rowAnchor(goal.place, row), effects);
          return false;
        }
        if (loading) return false;
        return this.searchOlder(goal, inputs, effects, () => {
          if (goal.fromSession) effects.dropSessionPlace();
          this.settle(TAIL, effects);
          return false;
        });
      }
    }
  }

  private searchOlder(
    goal: Extract<Goal, { kind: "first" | "row" }>, inputs: PlacementInputs,
    effects: PlacementEffects, fallback: () => boolean,
  ): boolean {
    // meiva:note
    // The time bound runs from the first pass that could not place the row, also while another
    // older read is still in flight and the search has not begun.
    if (goal.waitingSince === undefined) {
      this.goal = { ...goal, waitingSince: inputs.now };
      return true;
    }
    const { search } = goal;
    const oldest = findOldestTimelineCursor(inputs.items)?.item_id ?? null;
    const giveUp = () => {
      if (search) effects.cancelOlder();
      return fallback();
    };
    if (inputs.now - goal.waitingSince >= this.bounds.timeoutMs) return giveUp();
    if (search?.requested === oldest) {
      if (inputs.loadingOlder) {
        if (!search.sawLoading) this.goal = { ...goal, search: { ...search, sawLoading: true } };
        return false;
      }
      // meiva:note
      // Still waiting for the page to start, or it ended without reaching further back.
      return search.sawLoading || !inputs.hasMoreOlder ? giveUp() : false;
    }
    if (inputs.loadingOlder) return false;
    const pages = search?.pages ?? 0;
    const baseline = search?.baseline ?? oldest;
    const prepended = baseline === null ? 0 : Math.max(0, inputs.items.findIndex((item) => item.item_id === baseline));
    const usedForDelivery = goal.kind === "first" ? inputs.reveal.olderPages ?? 0 : 0;
    const budget = pages + usedForDelivery < this.bounds.maxPages && prepended < this.bounds.maxItems;
    if (!budget || !inputs.hasMoreOlder || !inputs.canLoadOlder || oldest === null || baseline === null) return giveUp();
    this.goal = { ...goal, search: { pages: pages + 1, requested: oldest, baseline, sawLoading: false } };
    effects.loadOlder();
    return false;
  }

  private publish(inputs: Pick<PlacementInputs, "items" | "compactBuffer" | "reveal" | "loading">): void {
    const requestedRow = this.requested?.anchor.kind === "row" ? this.requested.anchor.row : null;
    const next: PlacementSnapshot = {
      visible: this.goal.kind === "placed",
      searching: (this.goal.kind === "row" && !inputs.loading) || (this.goal.kind === "first" && this.goal.waitingSince !== undefined),
      pin: requestedRow ?? this.goalRow(inputs) ?? this.placedRow,
      fillAllowed: this.goal.kind === "placed" || (this.goal.kind === "first" && this.goal.search === undefined),
    };
    const prev = this.snap;
    const samePin = prev.pin === next.pin || (prev.pin !== null && next.pin !== null && sameRow(prev.pin, next.pin));
    if (prev.visible === next.visible && prev.searching === next.searching && prev.fillAllowed === next.fillAllowed && samePin) return;
    this.snap = next;
    for (const listener of this.listeners) listener();
  }
}
