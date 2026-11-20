import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { findOldestTimelineCursor } from "@/lib/timeline/timeline";

import type { ReloadRestoreConfig } from "../model/reading-position";
import { READING_TOP_PX, sameRow, TAIL, type RowRef, type ViewAnchor } from "./dom";
import { locate, rankOf } from "./rows";
import { readSessionPlace, rowAnchor, type SavedPlace } from "./saved";

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
  turnActive: boolean;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  canLoadOlder: boolean;
  reveal: RevealDecision;
  now: number;
}

export interface PlacementEffects {
  place: (anchor: ViewAnchor, options: { hold: boolean; sendReserve: boolean | null; smooth?: boolean }) => void;
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

type Goal =
  | { kind: "settings" }
  | { kind: "first"; readySince?: number }
  | { kind: "tail" }
  | { kind: "row"; place: SavedPlace; fromSession: boolean; search: Search | null; waitingSince?: number }
  | { kind: "placed" };

export interface PlacementSnapshot {
  visible: boolean;
  searching: boolean;
  pin: RowRef | null;
}

export function initialGoal(memory: SavedPlace | null, threadKey: string | undefined, reloadRestore: ReloadRestoreConfig): Goal {
  if (memory) return { kind: "row", place: memory, fromSession: false, search: null };
  if (threadKey === undefined) return { kind: "tail" };
  if (reloadRestore.state === "loading") return { kind: "settings" };
  const session = readSessionPlace(threadKey, reloadRestore);
  return session ? { kind: "row", place: session, fromSession: true, search: null } : { kind: "first" };
}

export class PlacementMachine {
  private goal: Goal;
  private requested: { anchor: ViewAnchor; smooth: boolean } | null = null;
  private placedRow: RowRef | null = null;
  private snap: PlacementSnapshot;
  private readonly listeners = new Set<() => void>();

  constructor(goal: Goal, private readonly bounds: SearchBounds) {
    this.goal = goal;
    this.snap = { visible: goal.kind === "placed", searching: false, pin: null };
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
    if (goal.kind === "first" && inputs.reveal.decided && inputs.reveal.itemId !== null) {
      const item = inputs.items.find((candidate) => candidate.item_id === inputs.reveal.itemId);
      return item ? { itemId: item.item_id, rank: rankOf(item, inputs.compactBuffer) } : null;
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
      this.placedRow = anchor.kind === "row" ? anchor.row : null;
      effects.place(anchor, { hold: false, sendReserve: null, smooth });
    }
    this.publish(inputs);
  }

  // meiva:note
  // A placement the reader asked for (quote, send, the arrow): lands once its row is mounted.
  request(anchor: ViewAnchor, inputs: PlacementInputs, smooth = false): void {
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
  lost(place: SavedPlace, inputs: PlacementInputs, effects: PlacementEffects): void {
    if (this.goal.kind !== "placed") return;
    const moved = locate(place.identity, inputs.items, inputs.compactBuffer);
    if (moved) {
      if (!sameRow(moved, place.identity.row)) effects.place(rowAnchor(place, moved), { hold: place.holdReserve, sendReserve: place.sendReserve });
      return;
    }
    this.goal = { kind: "row", place, fromSession: false, search: null };
    this.step(inputs, effects);
  }

  isPlaced(): boolean {
    return this.goal.kind === "placed";
  }

  private settle(target: ViewAnchor, options: { hold: boolean; sendReserve: boolean | null }, effects: PlacementEffects): void {
    this.goal = { kind: "placed" };
    this.placedRow = target.kind === "row" ? target.row : null;
    effects.place(target, options);
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
        if (loading && items.length === 0) return false;
        if (!inputs.reveal.decided) {
          // meiva:note
          // The rows are in but the reveal decision is not: wait for it, within the same time bound.
          if (goal.readySince === undefined) {
            this.goal = { kind: "first", readySince: inputs.now };
            return false;
          }
          if (inputs.now - goal.readySince < this.bounds.timeoutMs) return false;
        }
        const row = this.goalRow(inputs);
        // meiva:note
        // A first visit to a working conversation leaves room to align its last human message.
        this.settle(row ? { kind: "row", row, offset: READING_TOP_PX } : TAIL, { hold: false, sendReserve: inputs.turnActive }, effects);
        return false;
      }
      case "tail": {
        if (loading && items.length === 0) return false;
        this.settle(TAIL, { hold: false, sendReserve: false }, effects);
        return false;
      }
      case "row": {
        const row = this.goalRow(inputs);
        if (row) {
          this.settle(rowAnchor(goal.place, row), { hold: goal.place.holdReserve, sendReserve: goal.place.sendReserve }, effects);
          return false;
        }
        if (loading) return false;
        // meiva:note
        // The time bound runs from the first pass that could not place the row, also while another
        // older read is still in flight and the search has not begun.
        if (goal.waitingSince === undefined) {
          this.goal = { ...goal, waitingSince: inputs.now };
          return true;
        }
        const oldest = findOldestTimelineCursor(items)?.item_id ?? null;
        const search = goal.search;
        const giveUp = () => {
          if (search) effects.cancelOlder();
          if (goal.fromSession) effects.dropSessionPlace();
          this.settle(TAIL, { hold: false, sendReserve: false }, effects);
          return false;
        };
        if (inputs.now - goal.waitingSince >= this.bounds.timeoutMs) return giveUp();
        if (search?.requested === oldest) {
          if (inputs.loadingOlder) {
            if (!search.sawLoading) this.goal = { ...goal, search: { ...search, sawLoading: true } };
            return false;
          }
          // meiva:note
          // Still waiting for the page to start, or it ended without reaching further back.
          return search.sawLoading ? giveUp() : false;
        }
        if (inputs.loadingOlder) return false;
        const pages = search?.pages ?? 0;
        const baseline = search?.baseline ?? oldest;
        const prepended = baseline === null ? 0 : Math.max(0, items.findIndex((item) => item.item_id === baseline));
        const budget = pages < this.bounds.maxPages && prepended < this.bounds.maxItems;
        if (!budget || !inputs.hasMoreOlder || !inputs.canLoadOlder || oldest === null || baseline === null) return giveUp();
        this.goal = { ...goal, search: { pages: pages + 1, requested: oldest, baseline, sawLoading: false } };
        effects.loadOlder();
        return false;
      }
    }
  }

  private publish(inputs: Pick<PlacementInputs, "items" | "compactBuffer" | "reveal" | "loading">): void {
    const requestedRow = this.requested?.anchor.kind === "row" ? this.requested.anchor.row : null;
    const next: PlacementSnapshot = {
      visible: this.goal.kind === "placed",
      searching: (this.goal.kind === "row" && !inputs.loading) || (this.goal.kind === "first" && this.goal.readySince !== undefined),
      pin: requestedRow ?? this.goalRow(inputs) ?? this.placedRow,
    };
    const prev = this.snap;
    const samePin = prev.pin === next.pin || (prev.pin !== null && next.pin !== null && sameRow(prev.pin, next.pin));
    if (prev.visible === next.visible && prev.searching === next.searching && samePin) return;
    this.snap = next;
    for (const listener of this.listeners) listener();
  }
}
