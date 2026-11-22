"use client";

import { MessageSquare } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { GraphView } from "@/components/fleet/graph-view";
import { PluginNavIcons } from "@/components/plugins/plugin-nav";
import { useFleetAgents } from "@/lib/agents/use-fleet-agents";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";

// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
export function FleetView({ navigate }: { navigate: (href: Route) => void }) {
  const t = useTranslations("fleet");
  const agents = useFleetAgents();
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);
  const aliveCount = agents.filter((agent) => agent.status !== "terminated").length;

  return (
    <div className={cn("h-full bg-background text-foreground font-mono", FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0)}>
      <header className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2 lg:gap-3 lg:px-6 lg:py-3", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <span className="text-[11px] text-muted-foreground tabular-nums lg:text-xs">
          {t("activeTotal", { active: aliveCount, total: agents.length })}
        </span>
        <div className="ml-auto">
          <PluginNavIcons location="fleet-toolbar" />
        </div>
        <Link
          href="/"
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
          aria-label={t("backToConversation")}
        >
          <MessageSquare className="size-4 lg:size-5" aria-hidden />
        </Link>
      </header>
      <div className={cn("relative", FLEX_1, MIN_H_0, MIN_W_0)}>
        <div className="absolute inset-0">
          <GraphView
            selectedAgentId={selectedAgentId}
            onSelectAgent={setSelectedAgentId}
            navigate={navigate}
          />
        </div>
      </div>
    </div>
  );
}
