"use client";

import { useCallback, useLayoutEffect, useRef } from "react";

const PULL_THRESHOLD_PX = 64;
const AT_TOP_PX = 1;
const WHEEL_BURST_GAP_MS = 120;

interface WheelBurst {
  at: number;
  pull: number | "arrival" | "committed";
}

export function useOlderHistoryPull(
  threadKey: string | undefined,
  canLoad: () => boolean,
  loadPage: () => void,
  onUpwardIntent: () => void,
) {
  const latest = useRef({ canLoad, loadPage, onUpwardIntent });
  const wheelBurst = useRef<WheelBurst | null>(null);
  const touch = useRef<{ y: number; distance: number } | null>(null);

  useLayoutEffect(() => {
    wheelBurst.current = null;
    touch.current = null;
  }, [threadKey]);
  useLayoutEffect(() => {
    latest.current = { canLoad, loadPage, onUpwardIntent };
  });

  const onWheel = useCallback((event: WheelEvent, viewport: HTMLElement) => {
    if (event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY) || event.deltaY === 0) return;
    if (event.deltaY > 0) {
      wheelBurst.current = null;
      return;
    }

    latest.current.onUpwardIntent();
    const momentum = (event as WheelEvent & { readonly momentum?: boolean }).momentum;
    const burst = wheelBurst.current;
    const continuing = burst !== null && event.timeStamp - burst.at < WHEEL_BURST_GAP_MS;
    const atTop = viewport.scrollTop <= AT_TOP_PX;
    // meiva:note
    // A wheel burst must start at the top to page. After a request starts,
    // consume its remaining steps even if a fast response prepended rows.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
    if (momentum === true || (continuing && typeof burst.pull !== "number")) {
      if (atTop || burst?.pull === "committed") event.preventDefault();
      wheelBurst.current = { at: event.timeStamp, pull: burst?.pull === "committed" ? "committed" : "arrival" };
      return;
    }

    if (!atTop) {
      wheelBurst.current = { at: event.timeStamp, pull: "arrival" };
      return;
    }

    event.preventDefault();
    const pixels = -event.deltaY * (event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? viewport.clientHeight : 1);
    const distance = (continuing && typeof burst.pull === "number" ? burst.pull : 0) + pixels;
    const ready = distance >= PULL_THRESHOLD_PX;
    wheelBurst.current = { at: event.timeStamp, pull: ready ? "committed" : distance };
    if (ready && latest.current.canLoad()) latest.current.loadPage();
  }, []);

  const onTouchStart = useCallback((event: TouchEvent, viewport: HTMLElement) => {
    touch.current = event.touches.length === 1 && viewport.scrollTop <= AT_TOP_PX && latest.current.canLoad()
      ? { y: event.touches[0].clientY, distance: 0 } : null;
  }, []);
  const onTouchMove = useCallback((event: TouchEvent) => {
    const pull = touch.current;
    if (!pull || event.touches.length !== 1) { touch.current = null; return; }
    const nextY = event.touches[0].clientY;
    pull.distance += nextY - pull.y;
    pull.y = nextY;
    if (pull.distance <= 0) { touch.current = null; return; }
    latest.current.onUpwardIntent();
    event.preventDefault();
  }, []);
  const onTouchCancel = useCallback(() => { touch.current = null; }, []);
  const onTouchEnd = useCallback(() => {
    const pull = touch.current;
    onTouchCancel();
    if (pull && pull.distance >= PULL_THRESHOLD_PX && latest.current.canLoad()) latest.current.loadPage();
  }, [onTouchCancel]);

  return { onWheel, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel };
}
