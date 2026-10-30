"use client";

// meiva:note
// The horizontal TabBar is long gone: the whole page is Ctrl-F-searchable and
// every section is deep-linkable (#config-gateway, #skills, …). Named Control,
// not Settings — it carries operational actions (schedules, config, inventory),
// not just preferences.
//
// Every section's scaffolding AND its data queries render/fetch from first
// paint — see visibility.tsx for how that stays within the connection budget
// as the user scrolls. The section bodies below are the existing per-section
// components, unchanged except for threading `enabled: visible` into their
// queries.

import { ExternalLink, MessageSquare } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import type { ComponentProps, ComponentType } from "react";

import { useSettledAnchorScroll } from "./anchor-scroll";

import ConfigPage from "@/app/control/config/page";
import DisplaySettingsPage from "@/app/control/display/page";
import GuidePage from "@/app/control/guide/page";
import { McpInventory, PluginsInventory } from "@/app/control/inventory/page";
import SchedulesPage from "@/app/control/schedules/page";
import SkillsPage from "@/app/control/skills/page";
import { PluginNavList } from "@/components/plugins/plugin-nav";
import { buttonVariants } from "@/components/ui/button";
import { assetUrl } from "@/lib/transport/api";

import { AppSettingsEntry, useAppBridge } from "@/components/app/app-settings";

import { ControlNav } from "./nav";
import {
  APP_SETTINGS_SECTION,
  CONTROL_SCROLL_ID,
  CONTROL_SECTIONS,
} from "./sections";
import { ControlSection } from "./section";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { useBrowser } from "@/lib/browser/browser-provider";
import { cn } from "@/lib/format/utils";

type GuideSlot = ComponentType<ComponentProps<typeof GuidePage>>;
type ConfigSlot = ComponentType<ComponentProps<typeof ConfigPage>>;
type DisplaySlot = ComponentType<ComponentProps<typeof DisplaySettingsPage>>;
type PluginsSlot = ComponentType<ComponentProps<typeof PluginsInventory>>;
type McpSlot = ComponentType<ComponentProps<typeof McpInventory>>;
type SkillsSlot = ComponentType<ComponentProps<typeof SkillsPage>>;
type SchedulesSlot = ComponentType<ComponentProps<typeof SchedulesPage>>;

export interface ControlShellProps {
  Guide: GuideSlot;
  Config: ConfigSlot;
  Display: DisplaySlot;
  Plugins: PluginsSlot;
  Mcp: McpSlot;
  Skills: SkillsSlot;
  Schedules: SchedulesSlot;
}

// meiva:note
// This route is the Control page's composition root: it names the seven section bodies and hands
// them to the shell. The shell owns the layout, the nav and the anchor scroll, and takes each body
// as a required prop, so a test renders the shell with its own stand-ins instead of replacing the
// section modules.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
export default function ControlPage() {
  return (
    <ControlShell
      Guide={GuidePage}
      Config={ConfigPage}
      Display={DisplaySettingsPage}
      Plugins={PluginsInventory}
      Mcp={McpInventory}
      Skills={SkillsPage}
      Schedules={SchedulesPage}
    />
  );
}

export function ControlShell({ Guide, Config, Display, Plugins, Mcp, Skills, Schedules }: ControlShellProps) {
  const t = useTranslations("control");
  const ts = useTranslations("control.sections");
  const appBridge = useAppBridge();
  const sections = appBridge ? [...CONTROL_SECTIONS, APP_SETTINGS_SECTION] : CONTROL_SECTIONS;
  // meiva:note
  // Honor a #anchor on first load / direct link: resolve the target once,
  // from the URL hash at mount (later hash changes come from nav clicks,
  // which scroll themselves). The target scrolls into the container — no
  // smooth, it's the initial position, re-applied by useSettledAnchorScroll
  // while the async section bodies settle.
  const navigation = useBrowser().navigation;
  const [anchorTarget] = useState<string | null>(() => {
    const where = navigation.location();
    if (where === null) return null;
    const id = where.hash.slice(1);
    return id || null;
  });
  useSettledAnchorScroll(CONTROL_SCROLL_ID, anchorTarget);

  // meiva:note
  // Two notes for the tree below. The outer row clips (overflow-hidden) so the document/body never
  // gains a third scrollbar: the nav rail and the content column each own their vertical scroll,
  // so nothing should overflow the row even if a child mis-sizes. And the plugin pages
  // (contributions.ui.nav, location "settings") render beside the roster they belong to, absent
  // when nothing declares one.
  // meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
  return (
    <div className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("shrink-0 items-center gap-1.5 border-b border-border px-4 py-2", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <div className={cn(FLEX_1)} />
        <Link
          href="/"
          className={cn("items-center gap-1.5 rounded px-2 py-1 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label={t("backToAgents")}
        >
          <MessageSquare className="size-4 shrink-0" aria-hidden />
          <span className="hidden sm:inline">{t("backToAgents")}</span>
        </Link>
      </header>

      <div className={cn("md:flex-row", FLEX, FLEX_COL, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
        <ControlNav
          sections={sections}
          scrollId={CONTROL_SCROLL_ID}
          ariaLabel="Control sections"
        />
        <div id={CONTROL_SCROLL_ID} className={cn("overflow-y-auto overflow-x-hidden", FLEX_1)}>
          <div className={cn("mx-auto max-w-6xl space-y-12 px-6 py-6 text-sm", MIN_W_0)}>
            <ControlSection
              id="guide"
              label={ts("guide")}
              description={t("guideDescription")}
            >
              <Guide />
            </ControlSection>

            <ControlSection id="config" label={ts("config")}>
              <Config />
            </ControlSection>

            <ControlSection
              id="display"
              label={ts("display")}
              description={t("displayDescription")}
            >
              <Display />
            </ControlSection>

            <ControlSection
              id="plugins"
              label={ts("plugins")}
              description={t("pluginsDescription")}
            >
              <Plugins />
              <PluginNavList location="settings" />
            </ControlSection>

            <ControlSection
              id="mcp"
              label={ts("mcp")}
              description={t("mcpDescription")}
            >
              <Mcp />
            </ControlSection>

            <ControlSection
              id="skills"
              label={ts("skills")}
              description={t("skillsDescription")}
            >
              <Skills />
            </ControlSection>

            <ControlSection
              id="schedules"
              label={ts("schedules")}
              description={t("schedulesDescription")}
            >
              <Schedules />
            </ControlSection>

            {appBridge ? (
              <ControlSection
                id={APP_SETTINGS_SECTION.id}
                label={ts("app")}
                description={t("appDescription")}
              >
                <AppSettingsEntry bridge={appBridge} />
              </ControlSection>
            ) : null}

            <ControlSection
              id="okf-graph"
              label={ts("okf-graph")}
              description={t("okfGraphDescription")}
            >
              <a
                href={assetUrl("/api/okf/graph")}
                target="_blank"
                rel="noopener noreferrer"
                className={buttonVariants({ size: "sm", variant: "secondary" })}
              >
                <ExternalLink className="size-4" aria-hidden />
                {t("openOkfGraph")}
              </a>
            </ControlSection>
          </div>
        </div>
      </div>
    </div>
  );
}
