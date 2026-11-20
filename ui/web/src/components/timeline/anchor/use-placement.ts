"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { subscribeBeforeCompactDisplayChange, type CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { OlderLoadOrigin } from "@/lib/timeline/use-timeline";

import {
  RELOAD_RESTORE_MAX_OLDER_ITEMS,
  RELOAD_RESTORE_MAX_OLDER_PAGES,
  RELOAD_RESTORE_SEARCH_TIMEOUT_MS,
  type ReloadRestoreConfig,
} from "../model/reading-position";
import { READING_TOP_PX, readRowAnchor, sameRow, type RowRef, type ViewAnchor } from "./dom";
import { initialGoal, PlacementMachine, type PlacementEffects, type PlacementInputs, type RevealDecision, type SearchBounds } from "./placement";
import { identify, shiftForCompact, type RowIdentity } from "./rows";
import { readMemoryPlace, writeMemoryPlace, writeSessionPlace, type SavedPlace } from "./saved";
import type { AnchorChangeCause, ViewportAnchor } from "./use-viewport-anchor";

export type { RevealDecision } from "./placement";

const BOUNDS: SearchBounds = {
  maxPages: RELOAD_RESTORE_MAX_OLDER_PAGES,
  maxItems: RELOAD_RESTORE_MAX_OLDER_ITEMS,
  timeoutMs: RELOAD_RESTORE_SEARCH_TIMEOUT_MS,
};

interface Args {
  threadKey: string | undefined;
  entryKey: string | undefined;
  reloadRestore: ReloadRestoreConfig;
  items: readonly BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  loading: boolean;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder?: (origin: OlderLoadOrigin) => void;
  onCancelBackgroundLoadOlder?: () => boolean;
  reveal: RevealDecision;
  anchor: ViewportAnchor;
  filling: () => boolean;
}

export interface Placement {
  // meiva:note
  // The rows may be seen: the conversation's opening place has been reached.
  visible: boolean;
  // meiva:note
  // The cold fill may read older pages now.
  fillAllowed: boolean;
  // meiva:note
  // Older pages are being read to find the opening row.
  searching: boolean;
  // meiva:note
  // A row the window must mount for a placement in flight.
  pin: RowRef | null;
  // meiva:note
  // Place the reader at a row once it is mounted (quote, send, the arrow).
  request: (anchor: ViewAnchor, options?: { smooth?: boolean }) => void;
  onAnchorChange: (anchor: ViewAnchor, cause: AnchorChangeCause) => void;
  onUnresolved: (row: RowRef) => void;
  onFree: () => void;
}

// meiva:note
// Binds the placement machine (placement.ts) to the view. Every layout pass feeds it the loaded rows
// and the loading state; it then places the anchor, reads older pages, or drops an unreachable reload
// bookmark. Each anchor change is saved as the reader's place. Leaving saves the row on screen, even
// when following the tail (user ruling 2026-10-17), so a return reopens where the reader left rather
// than at newer output.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function usePlacement({
  threadKey, entryKey, reloadRestore, items, compactBuffer, loading, hasMoreOlder, loadingOlder,
  onLoadOlder, onCancelBackgroundLoadOlder, reveal, anchor, filling,
}: Args): Placement {
  const [machine] = useState(() => new PlacementMachine(initialGoal(readMemoryPlace(entryKey, threadKey), threadKey, reloadRestore), BOUNDS));
  const snapshot = useSyncExternalStore(machine.subscribe, machine.snapshot, machine.snapshot);

  const inputs: PlacementInputs = {
    threadKey, reloadRestore, items, compactBuffer, loading, hasMoreOlder, loadingOlder,
    canLoadOlder: Boolean(onLoadOlder), reveal, now: 0, filling,
  };
  const live = useRef({ inputs, onLoadOlder, onCancelBackgroundLoadOlder });
  useLayoutEffect(() => {
    live.current = { inputs, onLoadOlder, onCancelBackgroundLoadOlder };
  });
  const current = useCallback((): PlacementInputs => ({ ...live.current.inputs, now: Date.now() }), []);

  const effects = useMemo<PlacementEffects>(() => ({
    place: (target, { hold, smooth }) => anchor.place(target, { hold, smooth }),
    loadOlder: () => live.current.onLoadOlder?.("background"),
    cancelOlder: () => { live.current.onCancelBackgroundLoadOlder?.(); },
    dropSessionPlace: () => writeSessionPlace(threadKey, live.current.inputs.reloadRestore, null, 0),
  }), [anchor, threadKey]);

  // meiva:note
  // The rows a compact announcement saw: once a commit brings others, the re-keyed rows are in view.
  const rekeyItemsRef = useRef<readonly BackendTimelineItem[] | null>(null);
  useLayoutEffect(() => {
    if (rekeyItemsRef.current !== null && rekeyItemsRef.current !== items) {
      rekeyItemsRef.current = null;
      anchor.rekeySettled();
    }
    machine.step(current(), effects);
  });

  // meiva:note
  // A search whose page never answers ends at its time bound even without another commit.
  useEffect(() => {
    if (!snapshot.searching) return;
    const timer = setTimeout(() => machine.step(current(), effects), BOUNDS.timeoutMs);
    return () => clearTimeout(timer);
  }, [current, effects, machine, snapshot.searching]);

  // meiva:note
  // The anchor row's identity is captured while the row is loaded: a compact or a snapshot gap can
  // remove it, and its locator and coordinate are what find it again.
  const identityRef = useRef<RowIdentity | null>(null);
  // meiva:note
  // A compact re-ranks the rows before the store publishes the change: the anchor takes its new rank
  // at once (the new segment restarts message numbers, so the old id at the old rank may name another
  // row), and its identity is what finds the canonical row that later replaces it.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md
  useLayoutEffect(() => subscribeBeforeCompactDisplayChange((change) => {
    const now = anchor.current();
    if (now?.kind !== "row") return;
    const shifted = shiftForCompact(identify(now.row, change.items, change.buffer), change.rankShift);
    identityRef.current = shifted;
    rekeyItemsRef.current = live.current.inputs.items;
    anchor.rekey(shifted.row);
  }), [anchor]);

  const placeOf = useCallback((row: Extract<ViewAnchor, { kind: "row" }>, identity: RowIdentity): SavedPlace => ({
    identity,
    offset: row.offset,
    holdReserve: anchor.holdReserve(),
  }), [anchor]);

  const saveFrame = useRef(0);
  const onAnchorChange = useCallback((next: ViewAnchor, cause: AnchorChangeCause) => {
    const { items: rows, compactBuffer: buffer } = live.current.inputs;
    if (next.kind === "row") identityRef.current = identify(next.row, rows, buffer);
    if (cause === "reader") machine.readerMoved(current(), next.kind === "row" ? next.row : null);
    cancelAnimationFrame(saveFrame.current);
    saveFrame.current = requestAnimationFrame(() => {
      if (!machine.isPlaced()) return;
      const config = live.current.inputs.reloadRestore;
      const now = anchor.current();
      if (now?.kind === "tail") {
        writeSessionPlace(threadKey, config, null, 0);
        return;
      }
      if (now?.kind !== "row" || !identityRef.current) return;
      const place = placeOf(now, identityRef.current);
      writeMemoryPlace(entryKey, threadKey, place);
      writeSessionPlace(threadKey, config, place, anchor.viewport()?.scrollTop ?? 0);
    });
  }, [anchor, current, entryKey, machine, placeOf, threadKey]);

  const onUnresolved = useCallback((row: RowRef) => {
    const now = anchor.current();
    const identity = identityRef.current && sameRow(identityRef.current.row, row) ? identityRef.current : { row };
    const offset = now?.kind === "row" ? now.offset : READING_TOP_PX;
    machine.lost({ identity, offset, holdReserve: anchor.holdReserve() }, current(), effects);
  }, [anchor, current, effects, machine]);

  useLayoutEffect(() => () => {
    cancelAnimationFrame(saveFrame.current);
    if (!machine.isPlaced()) return;
    const vp = anchor.viewport();
    const now = anchor.current();
    const row = now?.kind === "row" ? now : vp ? readRowAnchor(vp) : null;
    if (!row) return;
    const identity = identityRef.current && sameRow(identityRef.current.row, row.row)
      ? identityRef.current
      : identify(row.row, live.current.inputs.items, live.current.inputs.compactBuffer);
    const place = placeOf(row, identity);
    writeMemoryPlace(entryKey, threadKey, place);
    if (now?.kind === "row") writeSessionPlace(threadKey, live.current.inputs.reloadRestore, place, vp?.scrollTop ?? 0);
  }, [anchor, entryKey, machine, placeOf, threadKey]);

  const onFree = useCallback(() => machine.readerMoved(current(), null), [current, machine]);

  const request = useCallback((target: ViewAnchor, options: { smooth?: boolean } = {}) => {
    machine.request(target, current(), options.smooth === true);
  }, [current, machine]);

  return {
    visible: snapshot.visible,
    fillAllowed: snapshot.fillAllowed,
    searching: snapshot.searching,
    // meiva:note
    // Before the first pass the machine has published nothing: the opening row is pinned from the start.
    pin: snapshot.pin ?? (snapshot.visible ? null : machine.goalRow(inputs)),
    request,
    onAnchorChange,
    onUnresolved,
    onFree,
  };
}
