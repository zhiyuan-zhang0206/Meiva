"use client";

import { CalendarClock, CircleHelp, Cpu, MessageSquare, Monitor, Palette, Plug, Puzzle, Settings2, SlidersHorizontal, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";
import type { ComponentProps, ComponentType } from "react";

import ConfigEditor from "./config/editor";
import DisplaySettingsPage from "./display/page";
import GuidePage from "./guide/page";
import { McpInventory, PluginsInventory } from "./inventory/page";
import SchedulesPage from "./schedules/page";
import SkillsPage from "./skills/page";
import { PluginNavList } from "@/components/plugins/plugin-nav";
import { AppSettingsEntry, useAppBridge } from "@/components/app/app-settings";
import { useBrowser } from "@/lib/browser/browser-provider";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { CONTROL_SCROLL_ID } from "./sections";
import { ControlSection } from "./section";
import { useSettledAnchorScroll } from "./anchor/anchor-scroll";

const PANELS = [
  { id: "guide", icon: CircleHelp, group: "guide" },
  { id: "config", icon: Settings2, group: "preferences" },
  { id: "display", icon: Palette, group: "preferences" },
  { id: "plugins", icon: Puzzle, group: "manage" },
  { id: "mcp", icon: Plug, group: "manage" },
  { id: "skills", icon: Sparkles, group: "manage" },
  { id: "schedules", icon: CalendarClock, group: "manage" },
  { id: "machines", icon: Monitor, group: "system" },
  { id: "advanced", icon: SlidersHorizontal, group: "system" },
  { id: "app", icon: Cpu, group: "system" },
] as const;
type PanelId = typeof PANELS[number]["id"];

function panelFor(anchor: string): PanelId {
  const match = PANELS.find((panel) => panel.id === anchor);
  if (match) return match.id;
  if (anchor === "config-default-model") return "config";
  if (anchor.startsWith("config-")) return "advanced";
  if (anchor.startsWith("display-")) return "display";
  return "config";
}

export interface ControlShellProps {
  Guide: ComponentType<ComponentProps<typeof GuidePage>>;
  Config: ComponentType<ComponentProps<typeof ConfigEditor>>;
  Display: ComponentType<ComponentProps<typeof DisplaySettingsPage>>;
  Plugins: ComponentType<ComponentProps<typeof PluginsInventory>>;
  Mcp: ComponentType<ComponentProps<typeof McpInventory>>;
  Skills: ComponentType<ComponentProps<typeof SkillsPage>>;
  Schedules: ComponentType<ComponentProps<typeof SchedulesPage>>;
}

export default function ControlPage() {
  return <ControlShell Guide={GuidePage} Config={ConfigEditor} Display={DisplaySettingsPage}
    Plugins={PluginsInventory} Mcp={McpInventory} Skills={SkillsPage} Schedules={SchedulesPage} />;
}

// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
export function ControlShell({ Guide, Config, Display, Plugins, Mcp, Skills, Schedules }: ControlShellProps) {
  const t = useTranslations("control");
  const ts = useTranslations("control.sections");
  const browser = useBrowser();
  const appBridge = useAppBridge();
  const [anchor, setAnchor] = useState(() => browser.navigation.location()?.hash.slice(1) ?? "config");
  const active = panelFor(anchor);
  const panels = PANELS.filter((panel) => panel.id !== "app" || appBridge);
  useSettledAnchorScroll(CONTROL_SCROLL_ID, anchor);

  useEffect(() => {
    const sync = () => setAnchor(browser.navigation.location()?.hash.slice(1) ?? "config");
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, [browser]);

  const select = (id: PanelId) => {
    setAnchor(id);
    browser.navigation.replace(`#${id}`, browser.navigation.state());
    document.getElementById(CONTROL_SCROLL_ID)?.scrollTo({ top: 0 });
  };
  const label = (id: PanelId) => id === "config" ? t("general") : id === "machines" ? t("machines")
    : id === "advanced" ? t("advanced") : ts(id);
  const description = active === "config" ? t("generalDescription")
    : active === "machines" ? t("machinesDescription")
    : active === "advanced" ? t("advancedDescription")
    : active === "display" ? t("displayDescription")
    : active === "schedules" ? t("schedulesDescription") : undefined;

  return (
    <div className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("shrink-0 items-center justify-between border-b border-border px-6 py-3", FLEX)}>
        <h1 className="text-base font-semibold">{t("title")}</h1>
        <Link href="/" className={cn("items-center gap-2 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-muted hover:text-foreground", FLEX)}>
          <MessageSquare className="size-4" aria-hidden />{t("backToAgents")}
        </Link>
      </header>
      <div className={cn("md:flex-row", FLEX, FLEX_COL, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
        <nav aria-label={t("navigation")} className="shrink-0 border-b border-border bg-muted/15 p-3 md:w-56 md:border-b-0 md:border-r md:overflow-y-auto">
          <div className={cn(FLEX, "gap-1 overflow-x-auto md:block md:space-y-5")}>
            {(["guide", "preferences", "manage", "system"] as const).map((group) => (
              <div key={group} className={cn(FLEX, "gap-1 md:block md:space-y-1")}>
                {group !== "guide" && <p className="hidden px-3 pb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground md:block">{t(group)}</p>}
                {panels.filter((panel) => panel.group === group).map(({ id, icon: Icon }) => (
                  <button key={id} type="button" onClick={() => select(id)} aria-current={active === id ? "page" : undefined}
                    className={cn(FLEX, "shrink-0 items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm md:w-full", active === id ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground")}>
                    <Icon className="size-4" aria-hidden />{label(id)}
                  </button>
                ))}
              </div>
            ))}
          </div>
        </nav>
        <div id={CONTROL_SCROLL_ID} className={cn("overflow-y-auto overflow-x-hidden", FLEX_1)}>
          <div className={cn("mx-auto max-w-4xl px-5 py-8 md:px-10", MIN_W_0)}>
            <ControlSection key={active} id={active} label={label(active)} description={description}>
              {active === "config" && <Config mode="general" />}
              {active === "advanced" && <Config mode="advanced" />}
              {active === "machines" && <Config mode="machines" />}
              {active === "display" && <Display />}
              {active === "plugins" && <><Plugins /><PluginNavList location="settings" /></>}
              {active === "mcp" && <Mcp />}
              {active === "skills" && <Skills />}
              {active === "schedules" && <Schedules />}
              {active === "guide" && <Guide />}
              {active === "app" && appBridge && <AppSettingsEntry bridge={appBridge} />}
            </ControlSection>
            {active === "config" && (
              <div className="mt-8 border-t border-border pt-5">
                <button type="button" onClick={() => select("advanced")} className="text-sm text-muted-foreground hover:text-foreground">{t("moreSettings")} →</button>
              </div>
            )}

          </div>
        </div>
      </div>
    </div>
  );
}
