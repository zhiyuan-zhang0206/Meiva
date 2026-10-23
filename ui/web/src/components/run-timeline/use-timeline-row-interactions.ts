"use client";

import { useEffect, useRef } from "react";

import { navigateAcross, type AgentSelection, type ViewAgent } from "./agent-view/agent-view-nav";
import type { LinkHit, LinkHitResult } from "./canvas/run-timeline-links-canvas";
import {
  endIn,
  nearestLink,
  nearestUnit,
  selectionMs,
  stepLink,
  type LinkRow,
  type ResolvedLink,
} from "./model/timeline-links";
import { panView, zoomView, type AxisMap, type Viewport } from "./model/timeline-model";
import { revealView, UNITS_ROW, type NavKey } from "./model/timeline-nav";

const DRAG_THRESHOLD_PX = 4;
const WHEEL_ZOOM_RATE = 0.0015;
const PINCH_ZOOM_RATE = 0.01;

export interface LinkGroupEntry {
  readonly row: LinkRow;
  readonly links: readonly ResolvedLink[];
}

export function useTimelineRowInteractions({
  chartRef,
  agents,
  view,
  base,
  axis,
  selection,
  navRow,
  setNavRow,
  setHover,
  onSelect,
  onView,
  groups,
  linkKey,
  onSelectLink,
  onSelectLinks,
  overItem,
  hitRef,
  hoverArrowTo,
}: {
  chartRef: React.RefObject<HTMLDivElement | null>;
  agents: ViewAgent[];
  view: Viewport;
  base: Viewport;
  axis: AxisMap;
  selection: AgentSelection | null;
  navRow: string | null;
  setNavRow: (row: string | null) => void;
  setHover: (hover: AgentSelection | null) => void;
  onSelect: (selection: AgentSelection) => void;
  onView: (view: Viewport) => void;
  groups: readonly LinkGroupEntry[];
  linkKey: string | null;
  onSelectLink: (key: string) => void;
  onSelectLinks: (keys: readonly string[]) => void;
  overItem: React.RefObject<boolean>;
  hitRef: React.RefObject<LinkHit>;
  hoverArrowTo: (next: LinkHitResult | null) => void;
}) {
  const live = useRef({ base, view, onView, axis });
  const pending = useRef<Viewport | null>(null);
  useEffect(() => {
    const wanted = pending.current;
    if (wanted && (view.from !== wanted.from || view.to !== wanted.to)) {
      live.current = { ...live.current, base, onView, axis };
      return;
    }
    pending.current = null;
    live.current = { base, view, onView, axis };
  });

  const nav = useRef({
    agents,
    view,
    base,
    axis,
    selection,
    navRow,
    setNavRow,
    setHover,
    onSelect,
    onView,
    groups,
    linkKey,
    onSelectLink,
  });
  useEffect(() => {
    nav.current = {
      agents,
      view,
      base,
      axis,
      selection,
      navRow,
      setNavRow,
      setHover,
      onSelect,
      onView,
      groups,
      linkKey,
      onSelectLink,
    };
  });

  useEffect(() => {
    const keys: Partial<Record<string, NavKey>> = {
      ArrowLeft: "left",
      ArrowRight: "right",
      ArrowUp: "up",
      ArrowDown: "down",
    };
    const onKey = (event: KeyboardEvent) => {
      const key = keys[event.key];
      if (
        event.defaultPrevented ||
        key === undefined ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        event.shiftKey
      ) {
        return;
      }
      const el = event.target instanceof Element ? event.target : null;
      if (
        el?.closest(
          "input, textarea, select, [contenteditable], [role=textbox], [role=separator], [role=slider], [role=combobox]",
        )
      ) {
        return;
      }
      const s = nav.current;
      const reveal = (ms: number) => {
        const shown = revealView(s.axis, s.view, s.base, ms, ms);
        if (shown !== s.view) s.onView(shown);
      };

      const at = s.linkKey === null ? -1 : s.groups.findIndex((g) => g.links.some((l) => l.key === s.linkKey));
      if (s.linkKey !== null && at >= 0) {
        event.preventDefault();
        const group = s.groups[at];
        const here = group.links.find((l) => l.key === s.linkKey);
        const hereMs = here === undefined ? 0 : (endIn(here, group.row)?.ms ?? 0);
        const goTo = (row: LinkRow, links: readonly ResolvedLink[], ms: number) => {
          const to = nearestLink(links, row, ms);
          const found = links.find((l) => l.key === to);
          if (to === null || found === undefined) return;
          s.onSelectLink(to);
          reveal(endIn(found, row)?.ms ?? ms);
        };
        if (key === "left" || key === "right") {
          const to = stepLink(group.links, s.linkKey, key);
          const found = group.links.find((l) => l.key === to);
          if (to !== null && found !== undefined) {
            s.onSelectLink(to);
            reveal(endIn(found, group.row)?.ms ?? hereMs);
          }
        } else if (key === "down") {
          const next = s.groups.at(at + 1);
          if (next !== undefined) goTo(next.row, next.links, hereMs);
        } else if (at > 0) {
          const prev = s.groups[at - 1];
          goTo(prev.row, prev.links, hereMs);
        } else {
          const last = s.agents.at(-1);
          const target = last === undefined ? null : nearestUnit(last.data, hereMs);
          if (last !== undefined && target !== null) {
            s.setNavRow(UNITS_ROW);
            s.onSelect({ agent: last.id, selection: target });
          }
        }
        return;
      }
      const next = navigateAcross(
        key,
        s.selection === null ? null : { ...s.selection, row: s.navRow },
        s.agents,
        s.axis,
        s.view,
      );
      event.preventDefault();

      if (el !== null && chartRef.current?.contains(el) && el instanceof HTMLElement) el.blur();
      s.setHover(null);
      if (next === null) {
        const last = s.agents.at(-1);
        const here =
          key === "down" && s.selection !== null && last?.id === s.selection.agent
            ? selectionMs(last.data, s.selection.selection)
            : null;
        const first = s.groups.at(0);
        const to = here === null || first === undefined ? null : nearestLink(first.links, first.row, here);
        const found = first?.links.find((l) => l.key === to);
        if (to !== null && found !== undefined && first !== undefined) {
          s.onSelectLink(to);
          reveal(endIn(found, first.row)?.ms ?? here ?? 0);
        }
        return;
      }
      s.setNavRow(next.row);
      s.onSelect({ agent: next.agent, selection: next.item.selection });
      const shown = revealView(s.axis, s.view, s.base, next.item.start, next.item.end);
      if (shown !== s.view) s.onView(shown);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chartRef]);

  const drag = useRef<{ x: number; view: Viewport; panning: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onWheel = (event: WheelEvent) => {
      const track = chart.querySelector("[data-track]")?.getBoundingClientRect();
      if (!track || track.width <= 0) return;
      const { base: b, view: v, axis: a } = live.current;
      const horizontal = !event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY);
      event.preventDefault();
      const rate = event.ctrlKey ? PINCH_ZOOM_RATE : WHEEL_ZOOM_RATE;
      const next = horizontal
        ? panView(a, v, b, event.deltaX / track.width)
        : zoomView(a, v, b, (event.clientX - track.left) / track.width, Math.exp(event.deltaY * rate));

      pending.current = next;
      live.current = { ...live.current, view: next };
      live.current.onView(next);
    };
    chart.addEventListener("wheel", onWheel, { passive: false });
    return () => chart.removeEventListener("wheel", onWheel);
  }, [chartRef]);

  const onLinks = useRef(onSelectLinks);
  useEffect(() => {
    onLinks.current = onSelectLinks;
  });
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onClick = (event: MouseEvent) => {
      if (
        overItem.current ||
        (event.target instanceof Element &&
          event.target.closest("button, input, select, textarea, a, [data-testid=run-timeline-link-tick]"))
      ) {
        return;
      }
      const hit = hitRef.current(event.clientX, event.clientY);
      if (hit !== null) onLinks.current(hit.members);
    };
    chart.addEventListener("click", onClick);
    return () => chart.removeEventListener("click", onClick);
  }, [chartRef, hitRef, overItem]);

  const trackRect = () => chartRef.current?.querySelector("[data-track]")?.getBoundingClientRect();
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const track = trackRect();
    if (event.button !== 0 || !track || event.clientX < track.left) return;
    drag.current = { x: event.clientX, view, panning: false, id: event.pointerId };
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    if (
      state === null &&
      !(event.target instanceof Element && event.target.closest("[data-testid=run-timeline-link-tick]"))
    ) {
      const hit = overItem.current ? null : hitRef.current(event.clientX, event.clientY);
      hoverArrowTo(hit);
      event.currentTarget.style.cursor = hit === null ? "" : "pointer";
    }
    const track = trackRect();
    if (!state || !track || track.width <= 0) return;
    const dx = event.clientX - state.x;
    if (!state.panning) {
      if (Math.abs(dx) < DRAG_THRESHOLD_PX) return;
      state.panning = true;
      event.currentTarget.setPointerCapture(state.id);
    }
    onView(panView(axis, state.view, base, -dx / track.width));
  };
  const endDrag = () => {
    if (drag.current?.panning) {
      suppressClick.current = true;
      setTimeout(() => {
        suppressClick.current = false;
      }, 0);
    }
    drag.current = null;
  };

  return { onPointerDown, onPointerMove, endDrag, suppressClick };
}
