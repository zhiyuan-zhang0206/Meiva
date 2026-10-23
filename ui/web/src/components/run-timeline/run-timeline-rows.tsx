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
import { INPUT_ROW, UNITS_ROW, levelRowId, navRowIds, type RowOptions } from "./model/timeline-nav-rows";
import { SELECTION_LINE_BELOW_PX, selectionRoles } from "./model/timeline-nav";
import { type AgentSelection, type ViewAgent } from "./agent-view/agent-view-nav";
import { AgentGroupHeader, AgentPending } from "./agent-view/agent-view-group";
import { useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import { LinkRowGroup } from "./agent-view/agent-view-link-group";
import { LinksCanvas, type LinkHit, type LinkHitResult } from "./canvas/run-timeline-links-canvas";
import { endIn, groupLinks, type LinkKind, type ResolvedLink } from "./model/links/resolve";
import { MAX_ARROWS } from "./model/links/cluster";
import { barTop, layoutsFor } from "./model/timeline-canvas-model";
import { frameOf, type RowLayout } from "./model/timeline-canvas-hit";
import { RunTimelineAxis } from "./canvas/run-timeline-axis";
import { TrackCanvas } from "./canvas/run-timeline-canvas";
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

export type AgentEntry =
  | { id: number; status: "loaded"; data: RunTimelineResponse }
  | { id: number; status: "loading" }
  | { id: number; status: "failed" };

// meiva:link ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md
export function RunTimelineRows({
  entries,
  base,
  view,
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
}: {

  entries: readonly AgentEntry[];

  base: Viewport;
  view: Viewport;
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
  const viewU = axis.viewU(view);
  const chartRef = useRef<HTMLDivElement>(null);
  const [trackPx, setTrackPx] = useState(DEFAULT_TRACK_PX);

  const layouts = new Map(agents.map((agent) => [agent.id, layoutsFor(agent.data, axis, viewU, trackPx, agent.rows)]));
  const selected = selection === null ? undefined : byId.get(selection.agent);
  const roles = selectionRoles(
    selection === null || selected === undefined ? null : { row: navRow, selection: selection.selection },
    selected?.data ?? EMPTY_DATA,
    selected?.rows,
  );

  const boxesOf = (agent: number, row: string, keys: Iterable<string>) => {
    const boxes = layouts.get(agent)?.get(row)?.boxes;
    return [...keys].flatMap((key) => boxes?.get(key) ?? []);
  };
  const primaryBoxes =
    roles.primary === null || selection === null ? [] : boxesOf(selection.agent, roles.primary.row, [roles.primary.key]);
  const primaryRaw = frameOf(primaryBoxes, trackPx);
  const lineX = primaryRaw !== null && primaryRaw.width < SELECTION_LINE_BELOW_PX ? primaryRaw.left + primaryRaw.width / 2 : null;
  const decoFor = (agent: number, row: string): RowDeco => {
    const mine = selection?.agent === agent;
    const primaryHere = mine && roles.primary?.row === row ? roles.primary.key : null;
    const linkedKeys = mine ? (roles.linked.get(row) ?? new Set<string>()) : new Set<string>();
    return {
      primaryKey: primaryHere,
      linkedKeys,
      primary: primaryHere === null ? null : frameOf(boxesOf(agent, row, [primaryHere]), trackPx),
      linked: frameOf(boxesOf(agent, row, linkedKeys), trackPx),
      lineX,
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
  const sources =
    highlight !== null && (highlight.cls === "human" || highlight.cls === "agent")
      ? [...new Set(agents.flatMap((agent) => inboundSources(agent.data.units, highlight.cls as "human" | "agent")))].sort()
      : [];

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
  ) => {
    const layout = layouts.get(agent.id)?.get(row);
    if (layout === undefined) return null;
    return (
      <TrackCanvas
        width={trackPx}
        height={height}
        layout={layout}
        paint={(p) => paint(p, layout)}
        testId={`run-timeline-canvas-${row}`}
        onHit={(key) => {
          const item = key === null ? undefined : layout.items.get(key);
          overItem.current = item !== undefined;
          setHover(item === undefined ? null : { agent: agent.id, selection: item.selection });
        }}
        onChoose={(key) => {
          const item = layout.items.get(key);
          if (item !== undefined) choose(agent.id, row, item.selection);
        }}
      />
    );
  };

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
            {canvasFor(agent, levelRowId(level), LEVEL_ROW_PX, (p, layout) =>
              paintNodes(p, layout, paintState, decoFor(agent.id, levelRowId(level))),
            )}
          </RowShell>
        ))}

        <RowShell label={t("messagesRow")} height="h-10" testId="run-timeline-row-units">
          {data.units_omitted > 0 ? (
            <p className="pointer-events-none absolute inset-x-0 top-0 z-10 bg-card/80 text-xs text-muted-foreground" data-testid="run-timeline-zoom-details">
              {t("zoomForDetails", { count: data.units_omitted })}
            </p>
          ) : null}
          {canvasFor(agent, UNITS_ROW, UNIT_ROW_PX, (p, layout) =>
            paintUnits(p, layout, paintState, decoFor(agent.id, UNITS_ROW), unitHeights, barTop(UNITS_ROW, data)),
          )}
        </RowShell>

        {agent.rows.includes(INPUT_ROW) ? (
          <RowShell label={t("contextRow")} height="h-10" testId="run-timeline-row-context">
            {canvasFor(agent, INPUT_ROW, CONTEXT_ROW_PX, (p, layout) =>
              paintBars(p, layout, barTop(INPUT_ROW, data), paintState, decoFor(agent.id, INPUT_ROW)),
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
      <LinksCanvas links={shownLinks} selectedKeys={selectedKeys} hoverKeys={hoverKeys} axis={axis} view={view} maxArrows={maxArrows} hitRef={hitRef} />
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
            viewU={viewU}
            selectedKey={linkKey}
            onHover={(key) => hoverArrowTo(key === null ? null : { key, members: [key] })}
            onSelect={onSelectLink}
            onClose={() => (row === "user" ? onShowUser(false) : onShowOther(false))}
          />
        ))}

      <RunTimelineAxis view={view} base={base} onView={onView} axis={axis} />

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
