"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { PackageDraftEntry } from "@/components/plugins/package-draft-entry";
import { InlineError } from "@/components/ui/inline-error";
import { Switch } from "@/components/ui/switch";
import { errMsg } from "@/lib/contracts/errors";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { useApi } from "@/lib/transport/transport-context";
import { useSectionVisible } from "../visibility";

const PLUGINS_QUERY_KEY = ["cluster-plugins"] as const;

export function PluginsInventory() {
  const api = useApi();
  const t = useTranslations("inventory");
  const qc = useQueryClient();
  const visible = useSectionVisible();
  const [saved, setSaved] = useState(false);
  const plugins = useQuery({ queryKey: PLUGINS_QUERY_KEY, queryFn: api.getPlugins, enabled: visible });
  const save = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) => api.putPluginEnabled(name, enabled),
    onSuccess: (result) => setSaved(result.restart_required),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: PLUGINS_QUERY_KEY });
      void qc.invalidateQueries({ queryKey: ["inventory"] });
      void qc.invalidateQueries({ queryKey: ["ui-contributions"] });
    },
  });
  return <div className="space-y-4">
    <PackageDraftEntry kind="plugin" />
    <p className="text-sm text-muted-foreground">{t("clusterPluginsHelp")}</p>
    {plugins.isLoading && <p className="text-sm text-muted-foreground">{t("loadingPlugins")}</p>}
    {plugins.error && <InlineError onRetry={() => void plugins.refetch()}>{t("couldntLoad")}</InlineError>}
    {plugins.data && <ul className="divide-y divide-border rounded-lg border border-border">
      {plugins.data.plugins.map((plugin) => <li key={plugin.name} className={cn(FLEX, "items-center justify-between gap-4 p-4")}>
        <div className={cn(MIN_W_0, "space-y-1")}>
          <p className="text-sm font-medium break-words">{plugin.name}</p>
          {plugin.description && <p className="text-xs text-muted-foreground">{plugin.description}</p>}
        </div>
        <Switch tone="neutral" checked={plugin.enabled} disabled={save.isPending}
          aria-label={t("togglePlugin", { name: plugin.name })}
          onCheckedChange={(enabled) => save.mutate({ name: plugin.name, enabled })} />
      </li>)}
    </ul>}
    {save.error && <InlineError>{t("saveFailed", { error: errMsg(save.error) })}</InlineError>}
    {saved && <p role="status" className="rounded-lg border border-border p-3 text-sm text-muted-foreground">{t("pluginRestart")}</p>}
  </div>;
}
