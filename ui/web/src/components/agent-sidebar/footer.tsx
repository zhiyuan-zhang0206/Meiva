"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Loader2,
  RotateCw,
  Settings,
  Waypoints,
} from "lucide-react";
import * as Popover from "@radix-ui/react-popover";
import { useTranslations } from "next-intl";

import { AlertsBadge } from "@/components/notifications/alerts-badge";
import { PluginNavIcons } from "@/components/plugins/plugin-nav";
import { WindowSelect } from "@/components/agents/window-select";
import { errMsg as formatErrMsg } from "@/lib/contracts/errors";
import { formatTokensCompact } from "@/lib/format/format-number";
import {
  formatRelativeTime,
  STATS_WINDOW_LABELS,
  STATS_WINDOWS,
  useStatsDashboard,
  useStatsWindow,
  type StatsWindowHours,
} from "@/lib/agents/sidebar";
import {
  usePluginStatCards,
  type PluginStatCard as PluginStatCardModel,
} from "@/lib/plugins/plugin-stats";
import type { StatsDashboard } from "@/lib/contracts/types";
import {
  FLEX,
  FLEX_COL,
  ICON_SIZE_CLASS,
  MIN_W_0,
} from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { useBrowser } from "@/lib/browser/browser-provider";
import { useNavigation } from "@/lib/browser/navigation/navigation-provider";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import { markNavIntent } from "@/lib/telemetry/nav-timing";

import { fleetHref } from "./links";
import { SidebarIconButton } from "./icon-button";

/** Shared by the expanded footer and collapsed rail. */
export const STATUS_POPOVER_CLASS =
  "z-50 max-h-[var(--radix-popover-content-available-height)] w-80 max-w-[calc(100vw-1.5rem)] overflow-y-auto rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none";

export function StatusCards({
  stats,
  error,
  fetching,
  windowHours,
  onWindowChange,
  onRetry,
}: {
  stats: StatsDashboard | undefined;
  error: unknown;
  fetching: boolean;
  windowHours: StatsWindowHours;
  onWindowChange: (h: StatsWindowHours) => void;
  onRetry: () => void;
}) {
  const t = useTranslations("sidebar");
  const now = useWallDate();
  const pluginCards = usePluginStatCards(stats?.plugin_stats, now);
  const errMsg = error ? formatErrMsg(error) : null;
  const failedWithoutData = errMsg !== null && stats === undefined;
  const firstLoad = stats === undefined && error === null && fetching;
  const placeholder = failedWithoutData ? "!" : "—";
  const win = STATS_WINDOW_LABELS[windowHours];
  const windowMismatch = stats !== undefined && stats.window_hours !== windowHours;
  const appliedWindowHours =
    !windowMismatch &&
    stats?.applied_window_hours != null &&
    stats.applied_window_hours < windowHours
      ? stats.applied_window_hours
      : null;
  const windowedPlaceholder = windowMismatch ? "…" : placeholder;
  const windowedTitle = windowMismatch ? t("statusPanelUpdatingFor", { win }) : null;
  const cards: (
    | { kind?: undefined; label: string; value: string; title?: string; windowed?: boolean }
    | { kind: "warnings"; title?: string; windowed?: boolean }
  )[] = [
    {
      label: t("liveAgents"),
      value: stats ? String(stats.live_count) : placeholder,
      title: errMsg ?? t("liveAgentsTitle"),
    },
    {
      label: t("tokens"),
      windowed: true,
      value: windowMismatch
        ? windowedPlaceholder
        : stats
        ? formatTokensCompact(stats.tokens.input + stats.tokens.output)
        : placeholder,
      title:
        windowedTitle ??
        errMsg ??
        (stats && stats.tokens.cache_hit_pct !== null
          ? t("tokensTitleDetail", { inp: stats.tokens.input.toLocaleString(), out: stats.tokens.output.toLocaleString(), cache: stats.tokens.cache_hit_pct })
          : t("tokensTitle", { win })),
    },
    {
      label: t("cacheHit"),
      windowed: true,
      value: windowMismatch
        ? windowedPlaceholder
        : stats
          ? stats.tokens.cache_hit_pct === null ? "N/A" : `${stats.tokens.cache_hit_pct.toFixed(2)}%`
          : placeholder,
      title: windowedTitle ?? errMsg ?? t("cacheHitTitle", { win }),
    },
    {
      label: t("cost"),
      windowed: true,
      value: windowMismatch
        ? windowedPlaceholder
        : stats
          ? `$${stats.cost_usd.toFixed(2)}`
          : placeholder,
      title:
        windowedTitle ??
        errMsg ??
        (stats
          ? t("costTitleDetail", { win, amount: stats.cost_usd })
          : t("costTitle", { win })),
    },
    {
      label: t("avgTurnTime"),
      windowed: true,
      value:
        windowMismatch
          ? windowedPlaceholder
          : stats?.avg_turn_seconds != null
          ? `${Math.round(stats.avg_turn_seconds)}s`
          : placeholder,
      title: windowedTitle ?? errMsg ?? t("avgTurnTitle", { win }),
    },
    {
      kind: "warnings",
      windowed: true,
      title: windowedTitle ?? errMsg ?? t("warningsTitle", { win }),
    },
  ];
  const valueClass = failedWithoutData
    ? "font-mono tabular-nums text-sm text-destructive"
    : "font-mono tabular-nums text-sm";
  const placeholderClass = failedWithoutData
    ? "font-mono tabular-nums text-sm text-destructive"
    : "font-mono tabular-nums text-sm";
  return (
    <div className="text-xs">
      <div className={cn("items-center justify-between border-b border-border px-3 py-2", FLEX)}>
        <div className={cn("items-center gap-1.5", FLEX)}>
          <span className="text-[10px] tracking-wide text-muted-foreground">
            {t("statusPanel")}
          </span>
          {fetching ? (
            <span
              role="status"
              aria-label={t("statusPanelUpdating")}
              title={t("statusPanelUpdating")}
            >
              <Loader2 className="size-3 animate-spin text-muted-foreground" aria-hidden />
            </span>
          ) : null}
          {failedWithoutData ? (
            <button
              type="button"
              onClick={onRetry}
              aria-label={t("statusPanelRetry")}
              title={errMsg}
              className="rounded p-0.5 text-destructive hover:bg-sidebar-accent"
            >
              <RotateCw className="size-3" aria-hidden />
            </button>
          ) : errMsg !== null ? (
            <span role="img" aria-label={errMsg} title={errMsg}>
              <AlertTriangle className="size-3 text-destructive" aria-hidden />
            </span>
          ) : null}
        </div>
        <WindowSelect
          value={String(windowHours)}
          options={STATS_WINDOWS.map((h) => ({
            value: String(h),
            label:
              h === windowHours && appliedWindowHours != null
                ? `${STATS_WINDOW_LABELS[h]} · ${appliedWindowHours}h`
                : STATS_WINDOW_LABELS[h],
          }))}
          // meiva:note
          // The select offers exactly the known windows, so the pick is looked up in that list: a
          // value the list does not hold changes nothing rather than being asserted into the type.
          // meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md
          onChange={(v) => {
            const picked = STATS_WINDOWS.find((h) => String(h) === v);
            if (picked !== undefined) onWindowChange(picked);
          }}
          ariaLabel={t("statusPanelWindow")}
          className="bg-transparent text-[10px] text-muted-foreground hover:text-foreground rounded px-1 py-0.5 cursor-pointer focus:outline-none"
        />
      </div>
      <div className="grid grid-cols-2 gap-1 px-3 py-2">
        {cards.map((card) =>
          card.kind === "warnings" ? (
            <WarningErrorCard
              key="warnings"
              stats={windowMismatch ? undefined : stats}
              placeholder={windowedPlaceholder}
              placeholderClass={placeholderClass}
              valueClass={valueClass}
              firstLoad={firstLoad}
              title={card.title}
            />
          ) : (
            <div
              key={card.label}
              title={windowMismatch && card.windowed ? card.title : undefined}
              className={cn("gap-0.5 px-2 py-1.5 rounded bg-sidebar-accent/40", FLEX, FLEX_COL)}
            >
              <span className="text-[10px] tracking-wide text-muted-foreground">
                {card.label}
              </span>
              {firstLoad ? (
                <span
                  className="h-4 w-10 animate-pulse rounded bg-muted-foreground/20"
                  aria-hidden
                />
              ) : (
                <span className={valueClass}>{card.value}</span>
              )}
            </div>
          ),
        )}
      </div>
      {pluginCards.length > 0 ? (
        // meiva:note
        // Plugin-declared cards (`contributions.ui.stats` + the values the
        // dashboard carried). Not windowed: a plugin value is a point in
        // time, so the window selector deliberately does not apply — the
        // section sits below the windowed grid precisely to say so.
        // meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md
        <div className="divide-y divide-border/60 border-t border-border px-3">
          {pluginCards.map((card) => (
            <PluginStatCard key={card.key} card={card} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// meiva:note
// One declared plugin row: label + the plugin's primary value + optional
// supporting text. Values and details wrap, preserving provider line breaks
// so reset schedules remain readable without a hover. The empty state ("—")
// means the card is declared but no value
// row exists yet (no credential, or no refresh has run); `stale` dims a value
// whose last write is old, so a stopped refresh cannot pass for a fresh one —
// the age itself rides the tooltip.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md
function PluginStatCard({ card }: { card: PluginStatCardModel }) {
  const t = useTranslations("sidebar");
  const now = useWallDate();
  const age = card.updatedAt !== null ? formatRelativeTime(card.updatedAt, now) : null;
  const title =
    card.value === null
      ? t("pluginStatNoData")
      : age !== null
        ? t("pluginStatUpdated", { ago: age })
        : undefined;
  return (
    <div
      title={title}
      className={cn(
        "gap-1 py-2.5",
        MIN_W_0,
        FLEX,
        FLEX_COL,
        card.stale && "opacity-60",
      )}
    >
      <span className="break-words text-xs font-medium text-muted-foreground">
        {card.label}
      </span>
      <span
        className={cn(
          "whitespace-pre-line break-words text-sm font-medium leading-5 tabular-nums",
          card.status === "error" && "text-destructive",
          card.status === "warn" && "text-amber-600 dark:text-amber-400",
        )}
      >
        {card.value ?? "—"}
      </span>
      {card.detail ? (
        <span className="whitespace-pre-line break-words text-xs leading-5 text-muted-foreground">
          {card.detail}
        </span>
      ) : null}
    </div>
  );
}

// meiva:note
// The spot an app's avatar row would occupy: Status (a chart icon that
// opens a small popover panel) on the left, and the four nav shortcuts
// (Memory Graph / Fleet / Insights / Control) on the right. These moved here
// from the header bar; the collapsed rail keeps icon-only versions.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md
export function SidebarFooter({ activeAgentId }: { activeAgentId: number | null }) {
  const t = useTranslations("sidebar");
  const navT = useTranslations("nav");
  const navigation = useNavigation();
  const { windowHours, setWindowHours } = useStatsWindow();
  const { stats, error: statsError, isFetching, refetch } = useStatsDashboard(windowHours);

  return (
    // meiva:note
    // The sidebar's own band, sized for its 28px icon row alone: rule (1) +
    // pt-2 (8) + row (28) + pb-3 (12) = 49px. Its rule and the composer's are
    // independent and never aligned by design (user ruling 2026-11-01), so
    // nothing here is derived from the composer's band — no shared primitive,
    // no minimum row height borrowed from the composer's controls, and no
    // tracking of the composer's measured height (user report 2026-10-29).
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/bottom-rules.meiva.okf.md
    <div
      data-testid="sidebar-footer"
      className="border-t border-border px-2 pt-2 pb-3"
    >
      <div className={cn("items-center justify-between", FLEX)}>
        <Popover.Root>
          <Popover.Trigger asChild>
            <button
              type="button"
              aria-label={t("statusPanel")}
              className="p-1.5 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
            >
              <BarChart3 className={ICON_SIZE_CLASS} />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content
              sideOffset={6}
              align="start"
              className={STATUS_POPOVER_CLASS}
            >
              <StatusCards
                stats={stats}
                error={statsError}
                fetching={isFetching}
                windowHours={windowHours}
                onWindowChange={setWindowHours}
                onRetry={() => { void refetch(); }}
              />
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>

        <div className={cn("items-center gap-0.5", FLEX)}>
          <AlertsBadge />
          <SidebarNavButton
            href={fleetHref(activeAgentId)}
            onClick={() => navigation.push(fleetHref(activeAgentId))}
            label={navT("fleet")}
          >
            <Waypoints className={ICON_SIZE_CLASS} />
          </SidebarNavButton>
          <SidebarNavButton href="/insights" onClick={() => navigation.push("/insights")} label={navT("insights")}>
            <Activity className={ICON_SIZE_CLASS} />
          </SidebarNavButton>
          <SidebarNavButton href="/control" onClick={() => navigation.push("/control")} label={navT("control")}>
            <Settings className={ICON_SIZE_CLASS} />
          </SidebarNavButton>
          <PluginNavIcons location="sidebar" />
        </div>
      </div>
    </div>
  );
}


/** One icon nav shortcut in the sidebar footer. */
function SidebarNavButton({
  onClick,
  label,
  href,
  children,
}: {
  onClick: () => void;
  label: string;
  href: string;
  children: React.ReactNode;
}) {
  const browser = useBrowser();
  return (
    <SidebarIconButton
      label={label}
      title={label}
      onClick={() => {
        markNavIntent(browser.navigation, href);
        onClick();
      }}
    >
      {children}
    </SidebarIconButton>
  );
}

// meiva:note
// Windowed Warning / Error event totals, presented like the other status cards.
// Formal Grafana alerts have their own header badge and history.
// meiva:link ui/web/src/docs/frontend-components/alert-classes.meiva.okf.md
function WarningErrorCard({
  stats,
  placeholder,
  placeholderClass,
  valueClass,
  firstLoad,
  title,
}: {
  stats: StatsDashboard | undefined;
  placeholder: string;
  placeholderClass: string;
  valueClass: string;
  firstLoad: boolean;
  title: string | undefined;
}) {
  const t = useTranslations("sidebar");
  return (
    <div title={title} className={cn("gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1.5", FLEX, FLEX_COL)}>
      <span className="text-[10px] tracking-wide text-muted-foreground">{t("warningsErrors")}</span>
      {firstLoad ? (
        <span className="h-4 w-10 animate-pulse rounded bg-muted-foreground/20" aria-hidden />
      ) : stats === undefined ? (
        <span className={placeholderClass}>{placeholder}</span>
      ) : (
        <span className={valueClass}>{`${stats.warnings} / ${stats.errors}`}</span>
      )}
    </div>
  );
}
