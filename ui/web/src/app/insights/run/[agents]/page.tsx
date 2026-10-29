"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import { ContextBreakdownCard, type CategoryHighlight } from "@/components/inspector/context-breakdown-body";
import { AgentViewToolbar } from "@/components/run-timeline/agent-view/agent-view-toolbar";
import { AgentPending } from "@/components/run-timeline/agent-view/agent-view-group";
import { useTimelineWindows } from "@/components/run-timeline/agent-view/use-timeline-windows";
import type { AgentSelection } from "@/components/run-timeline/agent-view/agent-view-nav";
import { NodeDetail, UnitDetail } from "@/components/run-timeline/run-timeline-detail";
import { LinkDetail } from "@/components/run-timeline/run-timeline-link-detail";
import { LinkGroupDetail } from "@/components/run-timeline/run-timeline-link-group-detail";
import { LINK_KINDS, resolveLinks, type LinkKind } from "@/components/run-timeline/model/links/resolve";
import { MAX_ARROWS } from "@/components/run-timeline/model/links/cluster";
import { RunTimelineRows, type AgentEntry } from "@/components/run-timeline/run-timeline-rows";
import { RunTimelineWorkspace } from "@/components/run-timeline/run-timeline-workspace";
import { useViewport } from "@/components/run-timeline/viewport/use-viewport";
import { viewportStore, type ViewportStore } from "@/components/run-timeline/viewport/viewport-store";
import { PageHeader } from "@/components/shell/page-header";
import { useTimeFormat } from "@/lib/format/use-time-format";
import { categoryClass, classCategory } from "@/components/run-timeline/model/timeline-model-classes";
import { clampViewport, MIN_VIEW_MS, viewportOf, type Viewport } from "@/components/run-timeline/model/timeline-model-viewport";
import { levelsTopFirst } from "@/components/run-timeline/model/timeline-model-selection";
import { nodeAncestors, nodeChildren, type Highlight } from "@/components/run-timeline/model/timeline-model-tree";
import type { UnitHeights } from "@/components/run-timeline/canvas/run-timeline-paint";
import type { RowOptions } from "@/components/run-timeline/model/timeline-nav-rows";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { useApi } from "@/lib/transport/transport-context";

function parseAgents(segment: string): number[] | null {
  const ids: number[] = [];
  for (const part of decodeURIComponent(segment).split(",")) {
    const id = Number(part);
    if (part.trim() === "" || !Number.isInteger(id) || id < 0) return null;
    if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

const NO_AGENTS: number[] = [];

const pathFor = (ids: readonly number[]) => `/insights/run/${ids.join(",")}`;

// meiva:note
// The URL preserves agents in addition order without duplicates and the last agent cannot be removed. Message indices belong to their snapshot: replacing it invalidates the selection before effects run. Block selection and arrow selection exclude each other. The context card follows only the selected agent and message/node, independently of the viewport. The page owns legend highlighting so zoom and pan retain it.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/window-reads.meiva.okf.md
export default function AgentViewPage({ params }: { params: Promise<{ agents: string }> }) {
  const client = useApi();
  const t = useTranslations("runTimeline");

  const [agentIds, setAgentIds] = useState<number[] | null>(null);
  const [paramsResolved, setParamsResolved] = useState(false);
  const [picked, setPicked] = useState<{ value: AgentSelection; snapshot: string | null | undefined } | null>(null);

  const [highlight, setHighlight] = useState<Highlight | null>(null);

  // meiva:note
  // The viewport lives in a store, not in this component's state: a wheel tick must not re-render the page,
  // the detail card or the rows. This page keeps only a debounced copy, for deciding what data to read.
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
  const [requestView, setRequestView] = useState<Viewport | null>(null);

  const [linkKeys, setLinkKeys] = useState<readonly string[]>([]);
  const setLinkKey = (key: string | null) => setLinkKeys(key === null ? [] : [key]);
  const [linkKinds, setLinkKinds] = useState<ReadonlySet<LinkKind>>(() => new Set(LINK_KINDS));
  const [showUser, setShowUser] = useState(true);
  const [showOther, setShowOther] = useState(true);
  const [maxArrows, setMaxArrows] = useState<number>(MAX_ARROWS);
  const [levels, setLevels] = useState<number | null>(null);

  const [contextSize, setContextSize] = useState(false);
  const [unitHeights, setUnitHeights] = useState<UnitHeights>("tokens");
  const options: RowOptions = useMemo(() => ({ levels, contextSize }), [levels, contextSize]);

  useEffect(() => {
    let cancelled = false;
    params
      .then(({ agents }) => {
        if (!cancelled) {
          setAgentIds(parseAgents(agents));
          setParamsResolved(true);
        }
      })
      .catch(() => {
        if (!cancelled) setParamsResolved(true);
      });
    return () => {
      cancelled = true;
    };
  }, [params]);

  const ids = agentIds ?? NO_AGENTS;
  const { reads, retry, initial } = useTimelineWindows(ids, requestView);

  const selection = picked !== null && picked.snapshot === initial.at(ids.indexOf(picked.value.agent))?.data?.snapshot ? picked.value : null;
  const setSelection = (next: AgentSelection | null) => setPicked(next === null ? null : {
    value: next, snapshot: initial.at(ids.indexOf(next.agent))?.data?.snapshot,
  });
  const setAgents = (next: number[]) => {
    setAgentIds(next);

    setRequestView(null);
    window.history.replaceState(null, "", pathFor(next));
  };
  const addAgent = (id: number) => {
    if (!ids.includes(id)) setAgents([...ids, id]);
  };
  const removeAgent = (id: number) => {
    if (ids.length <= 1) return;
    setLinkKey(null);
    if (selection?.agent === id) setSelection(null);
    setAgents(ids.filter((other) => other !== id));
  };

  const entries: AgentEntry[] = useMemo(
    () =>
      ids.map((id, i): AgentEntry => {
        const read = reads.at(i);
        if (read?.data !== undefined) return { id, status: "loaded", data: read.data };
        return { id, status: read?.isError ? "failed" : "loading" };
      }),
    [ids, reads],
  );
  const loaded = useMemo(
    () => entries.flatMap((entry) => (entry.status === "loaded" ? [{ id: entry.id, data: entry.data }] : [])),
    [entries],
  );

  const linkWindow = useMemo(() => {
    const spans = loaded.map(({ data }) => viewportOf(data.window));
    return spans.length === 0
      ? null
      : { from: new Date(Math.min(...spans.map((w) => w.from))).toISOString(), to: new Date(Math.max(...spans.map((w) => w.to))).toISOString() };
  }, [loaded]);
  const linksRead = useQuery({
    queryKey: ["run-timeline-links", [...ids].sort((a, b) => a - b).join(","), linkWindow?.from, linkWindow?.to],
    queryFn: () => client.getRunTimelineLinks(ids, linkWindow as { from: string; to: string }),
    enabled: linkWindow !== null,
  });
  const links = useMemo(
    () => resolveLinks(linksRead.data?.links ?? [], new Set(ids), new Map(loaded.map(({ id, data }) => [id, data]))),
    [linksRead.data, ids, loaded],
  );
  const selectedLinks = links.filter((l) => linkKeys.includes(l.key));

  const windows = initial.flatMap(({ data }) => data === undefined ? [] : [viewportOf(data.lifetime ?? data.window)]);
  const initialWindows = initial.flatMap(({ data }) => data === undefined ? [] : [viewportOf(data.window)]);
  // meiva:note
  // `base` and `view` keep their identity while their bounds are unchanged: the rows memoize their
  // layouts on the axis and the view, and a new object per render defeated that on every render that
  // had nothing to do with the viewport (task #5712).
  const baseFrom = windows.length === 0 ? null : Math.min(...windows.map((w) => w.from));
  const baseTo = windows.length === 0 ? null : Math.max(...windows.map((w) => w.to));
  const base: Viewport | null = useMemo(
    () => (baseFrom === null || baseTo === null ? null : { from: baseFrom, to: baseTo }),
    [baseFrom, baseTo],
  );
  const firstFrom = initialWindows.length === 0 ? null : Math.min(...initialWindows.map((w) => w.from));
  const firstTo = initialWindows.length === 0 ? null : Math.max(...initialWindows.map((w) => w.to));
  const firstView: Viewport | null = useMemo(
    () => (firstFrom === null || firstTo === null ? null : { from: firstFrom, to: firstTo }),
    [firstFrom, firstTo],
  );
  // meiva:note
  // The store outlives every data load; the effect below moves it to the view the loaded data implies, before
  // the frame that would paint anything, so no placeholder is ever drawn.
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
  const store = useMemo(() => viewportStore({ from: 0, to: 0 }), []);
  const wholeView = useMemo(
    () => (firstView === null ? null : base === null ? firstView : clampViewport(firstView, base)),
    [firstView, base],
  );
  useEffect(() => {
    if (wholeView !== null) store.set(wholeView);
  }, [store, wholeView]);
  // meiva:note
  // Every zoom and pan lands here: the store moves, the canvases paint in their next frame, and this page
  // re-renders only when the settled copy below asks for data it does not have (task #5738).
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
  const onView = (next: Viewport) => store.set(base === null ? next : clampViewport(next, base));
  const loadedKey = useMemo(() => initialWindows.map((window) => `${window.from}:${window.to}`).join(","), [initialWindows]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let handed = false;
    const flush = () => {
      timer = null;
      const next = store.get();
      // meiva:note
      // A gesture inside the data already loaded needs no read, so it must not re-render the page either: the
      // first real view still passes through, because the rows read one window around it before anyone moves.
      // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
      if (!(next.to > next.from)) return;
      const spans = loadedKey === "" ? [] : loadedKey.split(",").map((pair) => pair.split(":").map(Number));
      const covered = handed && spans.length > 0 && spans.every(([from, to]) => next.from >= from && next.to <= to);
      if (covered) return;
      handed = true;
      setRequestView((previous) =>
        previous !== null && previous.from === next.from && previous.to === next.to ? previous : next,
      );
    };
    const unsubscribe = store.subscribe(() => {
      timer ??= setTimeout(flush, VIEW_REQUEST_SETTLE_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [store, loadedKey]);

  // meiva:note
  // The invalid-URL answer comes after the hooks above: a return before them would render fewer hooks
  // than the valid page does.
  if (paramsResolved && agentIds === null) {
    return (
      <main id="main-content">
        <p className="p-6 font-mono text-sm text-destructive">{t("invalidAgent")}</p>
      </main>
    );
  }

  const dataOf = (id: number) => loaded.find((agent) => agent.id === id)?.data;

  const focus = selection !== null && dataOf(selection.agent) !== undefined ? selection.agent : loaded.at(0)?.id;
  const focusData = focus === undefined ? undefined : dataOf(focus);
  const mine = selection !== null && selection.agent === focus ? selection.selection : null;
  const selectedNode =
    mine?.kind === "node" ? focusData?.nodes.find((node) => node.id === mine.id) : undefined;
  const selectedUnit =
    mine?.kind === "unit"
      ? focusData?.units.find(
          (unit) => unit.i0 === mine.i0 && unit.i1 === mine.i1 && unit.kind === mine.unitKind,
        )
      : undefined;

  const contextAt = selectedUnit?.i0 ?? selectedNode?.span_start;
  const categoryHighlight: CategoryHighlight = {
    active: highlight === null ? null : classCategory(highlight.cls),
    has: (category) => categoryClass(category) !== null,
    onToggle: (category) => {
      const cls = categoryClass(category);
      if (cls === null) return;
      setHighlight(highlight?.cls === cls ? null : { cls, source: null });
    },
  };
  const maxLevels = Math.max(0, ...loaded.map(({ data }) => levelsTopFirst(data.nodes).length));

  const focusLink = (key: string) => {
    const target = links.find((l) => l.key === key);
    const shown = store.get();
    if (target === undefined) return;
    const times = selectedLinks.map((l) => Date.parse(l.link.ts));
    const width = Math.min(Math.max((Math.max(...times) - Math.min(...times)) * 20, MIN_VIEW_MS), shown.to - shown.from);
    const at = Date.parse(target.link.ts);
    onView({ from: at - width / 2, to: at + width / 2 });
    setSelection(null);
    setLinkKeys([key]);
  };
  const select = (agent: number) => (id: string) => setSelection({ agent, selection: { kind: "node", id } });

  const main = (
    <>
      <AgentViewToolbar
        agentIds={ids}
        onAdd={addAgent}
        levels={levels}
        maxLevels={maxLevels}
        onLevels={setLevels}
        contextSize={contextSize}
        onContextSize={setContextSize}
        unitHeights={unitHeights}
        onUnitHeights={setUnitHeights}
        showUser={showUser}
        onShowUser={(on) => {
          setShowUser(on);
          if (!on) setLinkKey(null);
        }}
        showOther={showOther}
        onShowOther={(on) => {
          setShowOther(on);
          if (!on) setLinkKey(null);
        }}
        maxArrows={maxArrows}
        onMaxArrows={setMaxArrows}
      />
      {reads.map((read, i) => read.preparing ? (
        <p key={ids[i]} role="status" className="text-xs text-muted-foreground">{t("preparingHistory", { id: ids[i] })}</p>
      ) : null)}
      {base && firstView ? (
        <>
          <div className="space-y-1 text-xs text-muted-foreground">
            <WindowSummary
              store={store}
              nodes={loaded.reduce((sum, { data }) => sum + data.nodes.length, 0)}
              units={loaded.reduce((sum, { data }) => sum + data.units.length + data.units_omitted, 0)}
            />
            {reads.map((read, i) => (
              <p
                key={ids[i]}
                role={read.windowError ? "alert" : "status"}
                data-testid={`run-timeline-window-status-${ids[i]}`}
                className={cn(FLEX, "h-4 items-center gap-1", read.windowError && "text-destructive")}
              >
                <span className="truncate">
                  {read.pending ? t("windowLoading", { id: ids[i] }) : read.windowError ? t("windowFailed", { id: ids[i] }) : ""}
                </span>
                {read.windowError && !read.pending ? <button type="button" className="shrink-0" onClick={() => retry(ids[i])}>{t("retry")}</button> : null}
              </p>
            ))}
          </div>
          <RunTimelineRows
            entries={entries}
            base={base}
            store={store}
            onView={onView}
            selection={selection}
            onSelect={(next) => {
              setLinkKey(null);
              setSelection(next);
            }}
            highlight={highlight}
            onHighlight={setHighlight}
            options={options}
            unitHeights={unitHeights}
            onRemove={ids.length > 1 ? removeAgent : null}
            onRetry={retry}
            links={links}
            showUser={showUser}
            onShowUser={(on) => {
              setShowUser(on);
              if (!on) setLinkKey(null);
            }}
            showOther={showOther}
            onShowOther={(on) => {
              setShowOther(on);
              if (!on) setLinkKey(null);
            }}
            linkKinds={linkKinds}
            onToggleLinkKind={(kind) =>
              setLinkKinds((kinds) => {
                const next = new Set(kinds);
                if (!next.delete(kind)) next.add(kind);
                return next;
              })
            }
            linkKeys={linkKeys}
            maxArrows={maxArrows}
            onSelectLinks={(keys) => {
              setSelection(null);
              setLinkKeys(keys);
            }}
          />
          {linksRead.isError ? (
            <p role="alert" className="text-xs text-destructive" data-testid="run-timeline-links-failed">
              {t("linksFailed")}
            </p>
          ) : null}
        </>
      ) : (
        <div className="space-y-3">
          {entries.map((entry) => (
            <AgentPending
              key={entry.id}
              agentId={entry.id}
              failed={entry.status === "failed"}
              onRetry={retry}
              onRemove={ids.length > 1 ? removeAgent : null}
            />
          ))}
        </div>
      )}

      {focus !== undefined && contextAt !== undefined ? (
        <ContextBreakdownCard agentId={focus} at={contextAt} snapshot={focusData?.snapshot ?? undefined} categoryHighlight={categoryHighlight} />
      ) : null}
    </>
  );

  const side = selectedLinks.length > 1 ? (
    <LinkGroupDetail links={selectedLinks} onSelect={focusLink} />
  ) : selectedLinks.length === 1 ? (
    <LinkDetail key={selectedLinks[0].key} resolved={selectedLinks[0]} snapshot={dataOf(selectedLinks[0].to.agent)?.snapshot ?? undefined} onAddAgent={addAgent} />
  ) : selectedNode ? (
    <NodeDetail
      key={`n${focus}-${selectedNode.id}`}
      agentId={focus ?? 0}
      snapshot={focusData?.snapshot ?? undefined}
      node={selectedNode}
      ancestors={focusData ? nodeAncestors(selectedNode, focusData.nodes) : []}
      childNodes={focusData ? nodeChildren(selectedNode, focusData.nodes) : []}
      onSelectNode={focus === undefined ? () => undefined : select(focus)}
    />
  ) : selectedUnit ? (
    <UnitDetail
      key={`u${focus}-${selectedUnit.kind}${selectedUnit.i0}-${selectedUnit.i1}`}
      agentId={focus ?? 0}
      snapshot={focusData?.snapshot ?? undefined}
      unit={selectedUnit}
      parent={focusData?.nodes.find((node) => node.id === selectedUnit.parent) ?? null}
      onSelectNode={focus === undefined ? () => undefined : select(focus)}
    />
  ) : (
    <p className="text-sm text-muted-foreground">{t("detailEmpty")}</p>
  );

  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <PageHeader title={t("title")} backHref="/insights" backLabel={t("backToInsights")} />
      <RunTimelineWorkspace main={main} side={side} />
    </main>
  );
}

const VIEW_REQUEST_SETTLE_MS = 80;

// meiva:note
// The line above the chart naming the visible range: it follows the store, so a wheel tick rewrites one
// sentence instead of re-rendering the page and its rows (task #5738).
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
function WindowSummary({ store, nodes, units }: { store: ViewportStore; nodes: number; units: number }) {
  const t = useTranslations("runTimeline");
  const { formatAbsolute } = useTimeFormat();
  const view = useViewport(store);
  return (
    <p data-testid="run-timeline-window">
      {t("windowSummary", {
        from: formatAbsolute(new Date(view.from).toISOString()),
        to: formatAbsolute(new Date(view.to).toISOString()),
        nodes,
        units,
      })}
    </p>
  );
}
