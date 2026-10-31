"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";
import { externalAgentName, EXTERNAL_AGENT_PREFIX } from "@/lib/format/inbound-source";

import { blockClass, type BlockClass } from "./model/timeline-model-classes";
import { hoverLit, inboundSources, type Highlight } from "./model/timeline-model-tree";
import { timeAxis } from "./model/timeline-model";
import { levelsTopFirst } from "./model/timeline-model-selection";
import { type Viewport } from "./model/timeline-model-viewport";
import { type ViewportStore } from "./viewport/viewport-store";
import { INPUT_ROW, UNITS_ROW, levelRowId, navRowIds, selectionKey, type RowOptions } from "./model/timeline-nav-rows";
import { SELECTION_LINE_BELOW_PX, selectionRoles } from "./model/timeline-nav";
import { type AgentSelection, type ViewAgent } from "./agent-view/agent-view-nav";
import { AgentGroupHeader, AgentPending } from "./agent-view/agent-view-group";
import { useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import { LinkRowGroup } from "./agent-view/agent-view-link-group";
import { LinksCanvas, type LinkHit, type LinkHitResult } from "./canvas/run-timeline-links-canvas";
import { endIn, groupLinks, type LinkKind, type ResolvedLink } from "./model/links/resolve";
import { MAX_ARROWS } from "./model/links/cluster";
import { barTop } from "./model/timeline-canvas-model";
import { frameOf, type RowLayout } from "./model/timeline-canvas-hit";
import { RunTimelineAxis } from "./canvas/run-timeline-axis";
import { TrackCanvas, type CanvasContextFactory } from "./canvas/run-timeline-canvas";
import { rowPaintKey, useThemeEpoch } from "./canvas/paint-key";
import { countRender } from "./model/render-stats";
import { paintBars, paintNodes, paintUnits, type PaintState, type RowDeco, type UnitHeights } from "./canvas/run-timeline-paint";
import { RunTimelineLegend } from "./run-timeline-legend";
import { RowShell } from "./run-timeline-row-shell";
import { readoutText } from "./model/run-timeline-readout";
import { useTimelineRowInteractions } from "./use-timeline-row-interactions";
import { useTimeFormat } from "@/lib/format/use-time-format";

const DEFAULT_TRACK_PX = 1000;

const LEVEL_ROW_PX = 32;
const UNIT_ROW_PX = 40;
const CONTEXT_ROW_PX = 40;
const EMPTY_DATA = { nodes: [], units: [] };
const EMPTY_KEYS: ReadonlySet<string> = new Set<string>();

export type AgentEntry =
  | { id: number; status: "loaded"; data: RunTimelineResponse }
  | { id: number; status: "loading" }
  | { id: number; status: "failed" };

// meiva:link ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md
export function RunTimelineRows({
  entries,
  base,
  store,
  onView,
  selection,
  onSelect,
  highlight,
  onHighlight,
  options,
  unitHeights,
  onRemove,
  onRetry,
  links,
  showUser,
  onShowUser,
  showOther,
  onShowOther,
  linkKinds,
  onToggleLinkKind,
  linkKeys,
  onSelectLinks,
  maxArrows = MAX_ARROWS,
  createContext,
}: {

  entries: readonly AgentEntry[];

  base: Viewport;
  store: ViewportStore;
  onView: (view: Viewport) => void;
  selection: AgentSelection | null;
  onSelect: (selection: AgentSelection) => void;

  highlight: Highlight | null;
  onHighlight: (highlight: Highlight | null) => void;
  options: RowOptions;

  unitHeights: UnitHeights;

  onRemove: ((agent: number) => void) | null;
  onRetry: (agent: number) => void;

  links: readonly ResolvedLink[];

  // meiva:note
  // How the two canvases obtain their drawing surface; omitted, each uses the page's own
  // `getContext("2d")`. A test that reads what was painted hands in a recording context.
  createContext?: CanvasContextFactory;

  showUser: boolean;
  onShowUser: (on: boolean) => void;
  showOther: boolean;
  onShowOther: (on: boolean) => void;
  linkKinds: ReadonlySet<LinkKind>;
  onToggleLinkKind: (kind: LinkKind) => void;

  linkKeys: readonly string[];
  onSelectLinks: (keys: readonly string[]) => void;

  maxArrows?: number;
}) {
  countRender();
  const linkKey = linkKeys.length === 1 ? linkKeys[0] : null;
  const onSelectLink = (key: string) => onSelectLinks([key]);
  const t = useTranslations("runTimeline");
  const { clockFormat } = useTimeFormat();
  const linkLabels = useLinkKindLabels();
  const [hover, setHover] = useState<AgentSelection | null>(null);

  const [hoverArrow, setHoverArrow] = useState<LinkHitResult | null>(null);
  const hoverArrowKey = useRef<string | null>(null);
  const hoverArrowTo = (next: LinkHitResult | null) => {
    if ((next?.key ?? null) === hoverArrowKey.current) return;
    hoverArrowKey.current = next?.key ?? null;
    setHoverArrow(next);
  };
  const hoverKeys = useMemo(() => new Set(hoverArrow?.members ?? []), [hoverArrow]);
  const selectedKeys = useMemo(() => new Set(linkKeys), [linkKeys]);

  const overItem = useRef(false);
  const hitRef = useRef<LinkHit>(() => null);
  const shownLinks = useMemo(
    () =>
      links.filter(
        (l) => linkKinds.has(l.kind) && (showUser || endIn(l, "user") === undefined) && (showOther || endIn(l, "other") === undefined),
      ),
    [showUser, showOther, links, linkKinds],
  );

  const groups = useMemo(() => {
    const grouped = groupLinks(shownLinks);
    return ([
      { row: "user", links: grouped.user },
      { row: "other", links: grouped.other },
    ] as const).filter((g) => g.links.length > 0);
  }, [shownLinks]);
  const linkCounts = useMemo(() => {
    const counts = new Map<LinkKind, number>();
    for (const l of links) counts.set(l.kind, (counts.get(l.kind) ?? 0) + 1);
    return counts;
  }, [links]);

  const [navRow, setNavRow] = useState<string | null>(null);
  const choose = (agent: number, row: string, target: AgentSelection["selection"]) => {
    setNavRow(row);
    onSelect({ agent, selection: target });
  };
  const loaded = useMemo(
    () =>
      entries.flatMap((entry) => (entry.status === "loaded" ? [{ id: entry.id, data: entry.data }] : [])),
    [entries],
  );
  const baseFrom = base.from;
  const baseTo = base.to;
  const axis = useMemo(() => timeAxis({ from: baseFrom, to: baseTo }), [baseFrom, baseTo]);
  const agents: ViewAgent[] = useMemo(
    () => loaded.map(({ id, data }) => ({ id, data, rows: navRowIds(data, options) })),
    [loaded, options],
  );
  const byId = new Map(agents.map((agent) => [agent.id, agent]));
  const chartRef = useRef<HTMLDivElement>(null);
  const [trackPx, setTrackPx] = useState(DEFAULT_TRACK_PX);
  const theme = useThemeEpoch();

  const selected = selection === null ? undefined : byId.get(selection.agent);
  const roles = selectionRoles(
    selection === null || selected === undefined ? null : { row: navRow, selection: selection.selection },
    selected?.data ?? EMPTY_DATA,
    selected?.rows,
  );

  const boxesOf = (layout: RowLayout, keys: Iterable<string>) =>
    [...keys].flatMap((key) => layout.boxes.get(key) ?? []);
  // meiva:note
  // The arrows that are lit and land on a block light that block with them, so an arrow's landing point
  // reads as one block instead of a place in a dense lane.
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md
  const linkTargets = new Map<number, Set<string>>();
  for (const arrow of [hoverArrow, linkKeys.length === 0 ? null : { members: linkKeys }]) {
    if (arrow === null) continue;
    const mine = new Set(arrow.members);
    for (const link of shownLinks) {
      if (!mine.has(link.key) || link.to.row !== UNITS_ROW || link.block === null) continue;
      const keys = linkTargets.get(link.to.agent) ?? new Set<string>();
      keys.add(selectionKey({ kind: "unit", i0: link.block.i0, i1: link.block.i1, unitKind: link.block.kind }));
      linkTargets.set(link.to.agent, keys);
    }
  }
  // meiva:note
  // The frame around a selection is drawn into the canvas, so it is worked out from the layout the paint
  // is about to use rather than from one a render happened to see (task #5738).
  const decoFor = (agent: number, row: string, layout: RowLayout): RowDeco => {
    const mine = selection?.agent === agent;
    const primaryHere = mine && roles.primary?.row === row ? roles.primary.key : null;
    const targets = row === UNITS_ROW ? (linkTargets.get(agent) ?? EMPTY_KEYS) : EMPTY_KEYS;
    const own = mine ? (roles.linked.get(row) ?? EMPTY_KEYS) : EMPTY_KEYS;
    const linkedKeys = targets.size === 0 ? own : new Set([...own, ...targets]);
    const primary = primaryHere === null ? null : frameOf(boxesOf(layout, [primaryHere]), trackPx);
    return {
      primaryKey: primaryHere,
      linkedKeys,
      primary,
      linked: frameOf(boxesOf(layout, linkedKeys), trackPx),
      lineX: primary !== null && primary.width < SELECTION_LINE_BELOW_PX ? primary.left + primary.width / 2 : null,
    };
  };
  useEffect(() => {
    const track = chartRef.current?.querySelector("[data-track]");
    if (!track) return;
    const measure = () => {
      const width = track.getBoundingClientRect().width;
      if (width > 0) setTrackPx(width);
    };
    measure();

    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(track);
    return () => observer.disconnect();
  }, []);

  const { onPointerDown, onPointerMove, endDrag, suppressClick } = useTimelineRowInteractions({
    chartRef,
    agents,
    store,
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
  });
  const classLabel: Record<BlockClass, string> = {
    human: t("blockHuman"),
    agent: t("blockAgent"),
    text: t("blockText"),
    thinking: t("blockThinking"),
    call: t("blockCall"),
    output: t("blockOutput"),
    note: t("blockNote"),
  };
  const unitLabel = (unit: Pick<RunTimelineUnit, "kind" | "source">) => classLabel[blockClass(unit)];
  const sourceLabel = (source: string) => {
    if (source.startsWith("agent:")) return t("sourceFromAgent", { id: source.slice("agent:".length) });
    if (source.startsWith(EXTERNAL_AGENT_PREFIX)) return t("sourceFromExternalAgent", { name: externalAgentName(source) });
    return source;
  };
  // meiva:note
  // The narrowed class is taken out of the highlight before the callback runs: a property read does
  // not keep its narrowing inside a closure, and naming the value is what makes the call safe.
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
  const sourceClass = highlight?.cls === "human" || highlight?.cls === "agent" ? highlight.cls : null;
  const sources =
    sourceClass === null ? [] : [...new Set(agents.flatMap((agent) => inboundSources(agent.data.units, sourceClass)))].sort();

  const describe = (target: AgentSelection | null) => {
    const owner = target === null ? undefined : byId.get(target.agent);
    if (target === null || owner === undefined) return null;
    const line = readoutText(target.selection, { data: owner.data, t, unitLabel, sourceLabel, clockFormat });
    return line !== null && agents.length > 1 ? t("readoutAgent", { id: target.agent, line }) : line;
  };
  const paintStateOf = (agent: ViewAgent): PaintState => {
    const mine = hover?.agent === agent.id ? hover.selection : null;
    return {
      selection: selection?.agent === agent.id ? selection.selection : null,
      hover: mine,
      lit: hoverLit(mine, agent.data.nodes, agent.data.units),
      highlight,
    };
  };
  const canvasFor = (
    agent: ViewAgent,
    row: string,
    height: number,
    paint: (p: Parameters<React.ComponentProps<typeof TrackCanvas>["paint"]>[0], layout: RowLayout) => void,
    extra: string,
  ) => (
    <TrackCanvas
      store={store}
      data={agent.data}
      rows={agent.rows}
      row={row}
      axis={axis}
      width={trackPx}
      height={height}
      createContext={createContext}
      paint={paint}
      paintKey={(layout) =>
        rowPaintKey({
          layout,
          selection: selection?.agent === agent.id ? selection.selection : null,
          hover: hover?.agent === agent.id ? hover.selection : null,
          highlight,
          deco: decoFor(agent.id, row, layout),
          width: trackPx,
          height,
          theme,
          extra,
        })
      }
      testId={`run-timeline-canvas-${row}`}
      onHit={(item) => {
        overItem.current = item !== null;
        setHover(item === null ? null : { agent: agent.id, selection: item.selection });
      }}
      onChoose={(item) => choose(agent.id, row, item.selection)}
    />
  );

  const spoken = describe(selection);
  const empty = loaded.every(({ data }) => data.nodes.length === 0 && data.units.length === 0 && data.units_omitted === 0);

  const renderAgent = (agent: ViewAgent) => {
    const { data } = agent;
    const paintState = paintStateOf(agent);
    const levels = levelsTopFirst(data.nodes).filter((level) => agent.rows.includes(levelRowId(level)));
    return (
      <section
        key={agent.id}
        aria-label={t("agentGroupAria", { id: agent.id })}
        data-testid={`agent-view-agent-${agent.id}`}
        className="space-y-1.5"
      >
        <AgentGroupHeader agentId={agent.id} onRemove={onRemove} />
        {levels.map((level) => (
          <RowShell
            key={level}
            label={t("levelRow", { level })}
            height="h-8"
            testId={`run-timeline-row-level-${level}`}
          >
            {canvasFor(
              agent,
              levelRowId(level),
              LEVEL_ROW_PX,
              (p, layout) => paintNodes(p, layout, paintState, decoFor(agent.id, levelRowId(level), layout)),
              "nodes",
            )}
          </RowShell>
        ))}

        <RowShell label={t("messagesRow")} height="h-10" testId="run-timeline-row-units">
          {data.units_omitted > 0 ? (
            <p className="pointer-events-none absolute inset-x-0 top-0 z-10 bg-card/80 text-xs text-muted-foreground" data-testid="run-timeline-zoom-details">
              {t("zoomForDetails", { count: data.units_omitted })}
            </p>
          ) : null}
          {canvasFor(
            agent,
            UNITS_ROW,
            UNIT_ROW_PX,
            (p, layout) => paintUnits(p, layout, paintState, decoFor(agent.id, UNITS_ROW, layout), unitHeights, barTop(UNITS_ROW, data)),
            `${unitHeights}:${barTop(UNITS_ROW, data).toFixed(3)}`,
          )}
        </RowShell>

        {agent.rows.includes(INPUT_ROW) ? (
          <RowShell label={t("contextRow")} height="h-10" testId="run-timeline-row-context">
            {canvasFor(
              agent,
              INPUT_ROW,
              CONTEXT_ROW_PX,
              (p, layout) => paintBars(p, layout, barTop(INPUT_ROW, data), paintState, decoFor(agent.id, INPUT_ROW, layout)),
              barTop(INPUT_ROW, data).toFixed(3),
            )}
          </RowShell>
        ) : null}
      </section>
    );
  };

  return (
    <div
      ref={chartRef}
      role="group"
      aria-label={t("chartAria")}
      data-testid="run-timeline-chart"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onPointerLeave={() => hoverArrowTo(null)}
      onClickCapture={(event) => {
        if (suppressClick.current) {
          event.stopPropagation();
          event.preventDefault();
        }
      }}
      className="relative select-none space-y-3 rounded-[10px] border border-border bg-card p-3"
    >
      <LinksCanvas
        links={shownLinks}
        selectedKeys={selectedKeys}
        hoverKeys={hoverKeys}
        axis={axis}
        store={store}
        maxArrows={maxArrows}
        geometryKey={`${theme}:${trackPx}:${loaded.map(({ id, data }) => `${id}@${data.snapshot}`).join(",")}`}
        createContext={createContext}
        hitRef={hitRef}
      />
      <p role="status" aria-live="polite" data-testid="run-timeline-selection-live" className="sr-only">
        {spoken ?? ""}
      </p>

      {entries.map((entry) => {
        if (entry.status !== "loaded") {
          return <AgentPending key={entry.id} agentId={entry.id} failed={entry.status === "failed"} onRetry={onRetry} onRemove={onRemove} />;
        }
        const agent = byId.get(entry.id);
        return agent === undefined ? null : renderAgent(agent);
      })}

      {(["user", "other"] as const)
        .filter((row) => (row === "user" ? showUser : showOther))
        .map((row) => (
          <LinkRowGroup
            key={row}
            variant={row}
            links={groupLinks(shownLinks)[row]}
            axis={axis}
            store={store}
            selectedKey={linkKey}
            onHover={(key) => hoverArrowTo(key === null ? null : { key, members: [key] })}
            onSelect={onSelectLink}
            onClose={() => (row === "user" ? onShowUser(false) : onShowOther(false))}
          />
        ))}

      <RunTimelineAxis store={store} base={base} onView={onView} axis={axis} />

      <RunTimelineLegend
        highlight={highlight}
        onHighlight={onHighlight}
        classLabel={classLabel}
        sources={sources}
        sourceLabel={sourceLabel}
        linkKinds={linkKinds}
        linkLabels={linkLabels}
        linkCounts={linkCounts}
        onToggleLinkKind={onToggleLinkKind}
      />

      {empty ? <p className="pt-1 text-xs text-muted-foreground">{t("empty")}</p> : null}
    </div>
  );
}
