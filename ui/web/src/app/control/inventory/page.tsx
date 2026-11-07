"use client";

// meiva:link gateway/extensions/docs/inventory.meiva.okf.md

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { PackageDraftEntry } from "@/components/plugins/package-draft-entry";
import { InlineError } from "@/components/ui/inline-error";
import { NativeSelect } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { errMsg } from "@/lib/contracts/errors";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { useApi } from "@/lib/transport/transport-context";
import { useSectionVisible } from "../visibility";
import { PluginsInventory } from "./_plugins";

export { PluginsInventory } from "./_plugins";

const INVENTORY_QUERY_KEY = ["inventory"] as const;

export function McpInventory() {
  const api = useApi();
  const t = useTranslations("inventory");
  const qc = useQueryClient();
  const visible = useSectionVisible();
  const [selected, setSelected] = useState("");
  const inventory = useQuery({ queryKey: INVENTORY_QUERY_KEY, queryFn: api.getInventory, enabled: visible });
  const save = useMutation({
    mutationFn: ({ machine, name, enabled }: { machine: string; name: string; enabled: boolean }) =>
      api.putInventory({ mcp_servers: { [name]: enabled } }, machine),
    onSettled: () => void qc.invalidateQueries({ queryKey: INVENTORY_QUERY_KEY }),
  });
  const data = inventory.data;
  const machine = data?.machines.includes(selected) ? selected : (data?.machines[0] ?? "");
  const unreachable = data?.unreachable.includes(machine) ?? false;
  const rows = data?.mcp_servers.filter((row) => Object.hasOwn(row.hosts, machine) && row.hosts[machine].present) ?? [];
  const rejected = save.data && !save.data.applied
    ? Object.values(save.data.mcp_results).find((result) => !result.ok)?.reason ?? t("cantEnable")
    : null;

  return <div className="space-y-4">
    <div className="space-y-2">
      <PackageDraftEntry kind="mcp" />
      <p className="text-xs text-muted-foreground">{t("mcpInstallScope")}</p>
    </div>
    <p className="text-sm text-muted-foreground">{t("machineMcpHelp")}</p>
    {inventory.isLoading && <Loader2 className="size-6 animate-spin text-muted-foreground" aria-label={t("loadingMcp")} />}
    {inventory.error && <InlineError onRetry={() => void inventory.refetch()}>{t("couldntLoad")}</InlineError>}
    {data && <>
      <label className="block space-y-2 text-sm">
        <span>{t("machine")}</span>
        <NativeSelect value={machine} onChange={(event) => { setSelected(event.target.value); save.reset(); }} disabled={data.machines.length === 0}>
          {data.machines.map((name) => <option key={name} value={name}>{name}</option>)}
        </NativeSelect>
      </label>
      {!machine ? <p className="text-sm text-muted-foreground">{t("noMachines")}</p>
        : unreachable ? <p role="status" className="text-sm text-muted-foreground">{t("machineUnreachable", { machine })}</p>
        : rows.length === 0 ? <p className="text-sm text-muted-foreground">{t("noMachineMcp")}</p>
        : <ul className="divide-y divide-border rounded-lg border border-border">
          {rows.map((row) => {
            const host = row.hosts[machine];
            const blocked = !host.enabled && host.can_enable === false;
            return <li key={row.name} className={cn(FLEX, "items-center justify-between gap-4 p-4")}>
              <div className={cn(MIN_W_0, "space-y-1")}>
                <p className="text-sm font-medium break-words">{row.name}</p>
                <p className="text-xs text-muted-foreground">{t(host.enabled ? "mcpEnabled" : "mcpDisabled")}</p>
                {host.reason && <p className="text-xs text-muted-foreground">{host.reason}</p>}
              </div>
              <Switch tone="neutral" checked={host.enabled} disabled={save.isPending || blocked}
                aria-label={blocked ? host.reason ?? t("cantEnable") : t("toggleMcp", { name: row.name, machine })}
                onCheckedChange={(enabled) => save.mutate({ machine, name: row.name, enabled })} />
            </li>;
          })}
        </ul>}
    </>}
    {save.error && <InlineError>{t("saveFailed", { error: errMsg(save.error) })}</InlineError>}
    {rejected && <InlineError>{t("changeNotApplied", { reason: rejected })}</InlineError>}
  </div>;
}

export default function InventoryPage() {
  const t = useTranslations("inventory");
  return <div className="space-y-6">
    <section>
      <h2 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">{t("plugins")}</h2>
      <PluginsInventory />
    </section>
    <section>
      <h2 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">{t("mcpServers")}</h2>
      <McpInventory />
    </section>
  </div>;
}
