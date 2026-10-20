"use client";

// meiva:note
// Agent processes only run on agent-runner machines (the gateway refuses a local
// spawn with HTTP 400, `gateway/agents/router.py:post_agents`), so the spawn
// target list requires online, unpaused agent-runner rows with an observed live
// host. Zero spawnable disables the button with a reason; one opens the table
// whose footer spawns there; several list the hosts in that footer, down entries
// disabled and explained. A gateway-hosted frontend never calls
// `onSpawn(undefined)` — the backend would read it as "spawn here" and reject it.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { DEFAULT_HARNESS, harnessRows, type Harness } from "@/lib/harness/harness";
import { useHarnessAvailabilityByMachine } from "@/lib/harness/use-harness-availability";
import { api } from "@/lib/transport/api";
import { SpawnTable } from "./spawn-table";
import { groupedModels, isSuperseded, runsOnHarness } from "@/lib/contracts/models";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

interface SpawnOpts {
  machine?: string;
  model?: string;
  reasoning_effort?: string;
  harness?: Harness;
}

// meiva:note
// Picks made while the form is open, laid over the stored spawn config. An
// empty object means "show the last spawn".
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
interface SpawnPicks {
  harness?: Harness;
  model?: string;
  effort?: string;
}

// meiva:note
// `onDone` lets the host popover close once the request is issued: the form
// never owns a popover of its own.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
interface Props {
  onSpawn: (opts: SpawnOpts) => void;
  onDone: () => void;
}

export function SpawnForm({ onSpawn, onDone }: Props) {
  const t = useTranslations("spawn");
  const { data: statusData } = useQuery({
    queryKey: ["status"],
    queryFn: () => api.getSystemStatus(),
    refetchInterval: 15_000,
  });
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => api.getModels(),
    // meiva:note
    // Config writes invalidate this catalog, including its effective birth default.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    staleTime: Infinity,
  });
  const modelsData = modelsQuery.error ? undefined : modelsQuery.data;
  const { settings, setSetting } = useUserSettings();
  // meiva:note
  // What the form shows is the LAST SPAWN, not the last click (user 2026-10-20):
  // `behavior.spawn_*` holds the config of the previous spawn, and a click only
  // lays a pick over it — written back at spawn, dropped when the form closes.
  // So reopening answers "what did the last agent run on", and a settings load
  // that lands after mount still reaches the form because nothing copies it.
  // `null` in the DB means "no override"; exposed here as `undefined`.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const storedHarness = (settings["behavior.spawn_harness"] as Harness | null) ?? DEFAULT_HARNESS;
  const storedModel = (settings["behavior.spawn_model"] as string | null) ?? undefined;
  const storedEffort = (settings["behavior.spawn_reasoning_effort"] as string | null) ?? undefined;
  const [picks, setPicks] = useState<SpawnPicks>({});
  const selectedHarness = picks.harness ?? storedHarness;
  const selectedModel = picks.model ?? storedModel;
  const selectedReasoningEffort = picks.effort ?? storedEffort;
  const pickHarness = (v: Harness) => setPicks((p) => ({ ...p, harness: v }));
  const pickModel = (v: string) => setPicks((p) => ({ ...p, model: v }));
  const pickEffort = (v: string) => setPicks((p) => ({ ...p, effort: v }));

  // meiva:note
  // Ops reachability is not agent-host readiness. Keep the existing host-alive
  // verdict's meaning, and show non-spawnable hosts in the form with a reason.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const allMachines = statusData?.cluster.machines ?? [];
  const eligible = allMachines.filter(
    (m) => m.serve_agent_runner && m.online && m.paused === false,
  );
  const spawnable = eligible.filter((m) => m.agent_host_online === true);
  // meiva:note
  // A harness a machine cannot start is not offered as a working spawn there: the
  // row is disabled and says only that it is unavailable, because an agent born on
  // a harness the machine cannot launch would sit inert while looking created.
  // Every candidate machine answers for itself - the spawn list names its target
  // only at click time, so one machine's probe says nothing about another's.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const harnessByMachine = useHarnessAvailabilityByMachine(spawnable.map((m) => m.name));
  // meiva:note
  // The table's harness column speaks for the host the next click would use: the
  // only spawnable machine, or the first of several before one is chosen.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const tableMachine = spawnable.at(0)?.name;
  const tableHarnessRows = harnessRows(tableMachine === undefined ? [] : harnessByMachine.get(tableMachine) ?? []);
  const harnessBlocked = (machine: string): boolean => {
    const row = harnessRows(harnessByMachine.get(machine) ?? [])
      .find((candidate) => candidate.harness === selectedHarness);
    return row !== undefined && !row.launchable;
  };

  // meiva:note
  // Models the chosen harness can run: user-hidden models (setting models.hidden)
  // and registry-superseded models are excluded, and the model's published
  // `harnesses` decides which harnesses offer it.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const hiddenModels: string[] = (settings["models.hidden"] as string[] | undefined) ?? [];
  const rosterModels = new Set(Object.values(modelsData?.providers ?? {}).flat());
  const hiddenModelCount = new Set(
    hiddenModels.filter((model) => rosterModels.has(model)),
  ).size;
  const modelGroups = groupedModels(
    modelsData,
    (m) =>
      !hiddenModels.includes(m) &&
      !isSuperseded(modelsData, m) &&
      runsOnHarness(modelsData, m, selectedHarness),
  );
  const defaultModel = modelsData?.default;

  // meiva:note
  // The model the next agent will be born on: the stored choice while the chosen
  // harness can run it, else this harness's first model (the stored value stays,
  // so switching the harness back restores it). Undefined only when the catalog
  // offers nothing for that harness.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const visibleModels = modelGroups.flatMap(([, models]) => models);
  const effectiveModel: string | undefined =
    selectedModel !== undefined && visibleModels.includes(selectedModel)
      ? selectedModel
      : defaultModel !== undefined && visibleModels.includes(defaultModel)
        ? defaultModel
        : visibleModels[0];

  const selectedModelInfo = effectiveModel ? modelsData?.models[effectiveModel] : undefined;
  // meiva:note
  // The effort ladder this model offers, with all three "no ladder" shapes
  // collapsed into one empty list: the model carries no effort knob (wire null),
  // the models query has not resolved yet, and the resolved name is absent from
  // the catalog.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const effortLevels = selectedModelInfo?.reasoning_effort_options ?? [];
  // meiva:note
  // The model's concrete default effort, published by GET /api/models from the
  // registry's per-model tuning (every catalog model pins one — validated
  // server-side). The table pre-selects it, so selecting a model immediately
  // marks the level that model runs at; there is no synthetic "Effort: default"
  // row.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const modelDefaultEffort =
    typeof selectedModelInfo?.reasoning_effort_default === "string"
      ? selectedModelInfo.reasoning_effort_default
      : null;
  // meiva:note
  // Re-derived from the effective model on every render rather than trusted from
  // the stored setting: a level the current model does not offer must not reach
  // the spawn request. Explicit selection wins, then the model's own default; a
  // model the catalog has not resolved publishes no default, and nothing is sent
  // for it. The stored setting is left as the user last set it, so switching back
  // to a model that offers it restores it.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const effectiveReasoningEffort: string | undefined =
    selectedReasoningEffort !== undefined && effortLevels.includes(selectedReasoningEffort)
      ? selectedReasoningEffort
      : (modelDefaultEffort ?? undefined);

  // meiva:note
  // The spawn is the only write: it stores the config the request actually used
  // (cascade included), so the next open shows the last spawn. Unchanged values
  // write nothing. Releasing the draft afterwards lets the stored values — now
  // this spawn's — re-seed it, so a cascade-filled model or effort becomes the
  // visible pick instead of staying an invisible default.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  function rememberSpawnedConfig() {
    if (selectedHarness !== storedHarness) setSetting("behavior.spawn_harness", selectedHarness);
    if (effectiveModel !== undefined && effectiveModel !== storedModel) {
      setSetting("behavior.spawn_model", effectiveModel);
    }
    if (effectiveReasoningEffort !== undefined && effectiveReasoningEffort !== storedEffort) {
      setSetting("behavior.spawn_reasoning_effort", effectiveReasoningEffort);
    }
    setPicks({});
  }

  function buildOpts(machine?: string): SpawnOpts {
    return {
      machine,
      model: effectiveModel,
      reasoning_effort: effectiveReasoningEffort,
      // meiva:note
      // The built-in runtime is the server's own default: naming it adds nothing,
      // and an omitted field keeps a spawn request byte-identical to the old one.
      // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
      ...(selectedHarness === DEFAULT_HARNESS ? {} : { harness: selectedHarness }),
    };
  }

  function spawnOn(machine: string) {
    rememberSpawnedConfig();
    onSpawn(buildOpts(machine));
    onDone();
  }

  if (modelsQuery.error) {
    return <div role="status" className="px-3 py-2 text-xs text-muted-foreground">{t("modelsUnavailable")}</div>;
  }

  if (spawnable.length === 0) {
    const reason = eligible.length === 1
      ? eligible[0].agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")
      : t("noReadyRunner");
    return <div role="status" className="px-3 py-2 text-xs text-muted-foreground">{reason}</div>;
  }

  // meiva:note
  // One row offers one machine: a host that is down and a harness that machine
  // cannot start are both refusals, and the row shows the first that applies so
  // the button never explains one while blocking for the other.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const sorted = [...eligible].sort((a, b) => a.name.localeCompare(b.name));
  const machineRow = (m: (typeof sorted)[number]) => {
    const hostReason = m.agent_host_online === true
      ? undefined
      : m.agent_host_online === false ? t("hostUnavailable") : t("hostUnknown");
    const harnessReason = harnessBlocked(m.name) ? t("harnessStatus.unavailable") : undefined;
    const reason = hostReason ?? harnessReason;
    return (
      <li key={m.name}>
        <button
          type="button"
          disabled={reason !== undefined}
          title={reason}
          onClick={() => spawnOn(m.name)}
          className={cn("w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent disabled:opacity-50 items-center justify-between gap-3", FLEX)}
        >
          <span className="truncate">{m.name}</span>
          {reason !== undefined && <span className="text-xs">{reason}</span>}
        </button>
      </li>
    );
  };

  const single = spawnable.length === 1 && eligible.length === 1 ? spawnable[0] : undefined;

  return (
    <div className="rounded-md border border-border bg-popover text-popover-foreground">
      <SpawnTable
        harnessRows={tableHarnessRows}
        harness={selectedHarness}
        onHarness={pickHarness}
        modelGroups={modelGroups}
        models={modelsData?.models}
        model={effectiveModel}
        onModel={pickModel}
        efforts={effortLevels}
        effort={effectiveReasoningEffort}
        onEffort={pickEffort}
      />
      {hiddenModelCount > 0 && (
        <div className="border-t border-border px-3 py-1.5 text-[11px] text-muted-foreground">
          {t("hiddenModels", { count: hiddenModelCount })}
        </div>
      )}
      <div className="border-t border-border">
        {single !== undefined ? (
          <button
            type="button"
            disabled={harnessBlocked(single.name)}
            title={harnessBlocked(single.name) ? t("harnessStatus.unavailable") : undefined}
            onClick={() => spawnOn(single.name)}
            className={cn(
              "w-full items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium hover:bg-sidebar-accent disabled:opacity-50",
              FLEX,
            )}
          >
            <Plus className="size-3.5" />
            <span>{t("spawn")}</span>
          </button>
        ) : (
          <>
            <div className="px-3 py-1.5 text-xs font-medium text-muted-foreground">{t("spawnOn")}</div>
            <ul className="max-h-[180px] overflow-y-auto py-1">{sorted.map(machineRow)}</ul>
          </>
        )}
      </div>
    </div>
  );
}
