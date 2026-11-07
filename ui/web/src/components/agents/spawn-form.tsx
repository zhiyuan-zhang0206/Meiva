"use client";

// meiva:note
// Agent processes only run on agent-runner machines (the gateway refuses a local
// spawn with HTTP 400, `gateway/agents/router.py:post_agents`), so the spawn
// target list requires online, unpaused agent-runner rows with an observed live
// host. Zero spawnable disables the button with a reason; one opens the table
// whose footer spawns there; several list the hosts in that footer, down entries
// disabled and explained. A gateway-hosted frontend never calls
// `onSpawn(undefined)` — the backend would read it as "spawn here" and reject it.
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md

import { useIsMutating, useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { InlineError } from "@/components/ui/inline-error";
import { DEFAULT_HARNESS, HARNESSES, harnessRows, type Harness } from "@/lib/harness/harness";
import { useHarnessAvailabilityByMachine, useRefreshHarnessAvailability } from "@/lib/harness/use-harness-availability";
import { SpawnStack, SpawnTable, type SpawnMachine } from "./spawn-table";
import { groupedModels, isSuperseded } from "@/lib/contracts/models";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { SYSTEM_STATUS_QUERY_KEY } from "@/lib/notifications/use-cluster-health";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { useApi } from "@/lib/transport/transport-context";

// meiva:note
// The readers for a `Record<string, unknown>` settings row: each answers the value only when it is
// the type the field is declared with, so no call site has to assert one.
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
function readString(settings: Record<string, unknown>, key: string): string | undefined {
  const value = settings[key];
  return typeof value === "string" ? value : undefined;
}

function readStringList(settings: Record<string, unknown>, key: string): string[] {
  const value = settings[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function readHarness(settings: Record<string, unknown>, key: string): Harness | undefined {
  const value = settings[key];
  return HARNESSES.find((harness) => harness === value);
}

interface SpawnOpts {
  machine?: string;
  model?: string;
  reasoning_effort?: string;
  harness?: Harness;
}

// meiva:note
// Picks made while the form is open, laid over the stored spawn config. An
// empty object means "show the last spawn".
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
interface SpawnPicks {
  machine?: string;
  harness?: Harness;
  model?: string;
  effort?: string;
}

// meiva:note
// `onDone` lets the host popover close once the request is issued: the form
// never owns a popover of its own.
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
interface Props {
  onSpawn: (opts: SpawnOpts) => void;
  onDone: () => void;
  // meiva:note
  // The shell owns the arrangement: below md the popover is 94vw and the
  // four-column table leaves the model column 4px (2026-10-21), so the same
  // decisions render stacked. The form keeps one rule set either way.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  layout?: "table" | "stack";
}

export function useSpawnStatus(poll: boolean) {
  const client = useApi();
  return useQuery({
    queryKey: SYSTEM_STATUS_QUERY_KEY,
    queryFn: () => client.getSystemStatus(),
    refetchInterval: poll ? 15_000 : false,
    staleTime: 15_000,
  });
}

export function SpawnForm({ onSpawn, onDone, layout = "table" }: Props) {
  const client = useApi();
  const t = useTranslations("spawn");
  const statusQuery = useSpawnStatus(true);
  const statusData = statusQuery.data;
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => client.getModels(),
    // meiva:note
    // Config writes invalidate this catalog, including its effective birth default.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    staleTime: Infinity,
  });
  const modelsData = modelsQuery.error ? undefined : modelsQuery.data;
  const refresh = useRefreshHarnessAvailability();
  const refreshing = useIsMutating({ mutationKey: ["harness-refresh"] }) > 0;
  const { settings, setSetting } = useUserSettings();
  // meiva:note
  // What the form shows is the LAST SPAWN, not the last click (user 2026-10-20):
  // `behavior.spawn_*` holds the config of the previous spawn, and a click only
  // lays a pick over it — written back at spawn, dropped when the form closes.
  // So reopening answers "what did the last agent run on", and a settings load
  // that lands after mount still reaches the form because nothing copies it.
  // `null` in the DB means "no override"; exposed here as `undefined`.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  // meiva:note
  // `settings` is the user_settings row as it comes off the wire (`Record<string, unknown>`), so each
  // field states the value it accepts and falls back when the stored one is not that: a harness the
  // build does not know reads as the default, and a non-string model reads as "no override".
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const storedHarness = readHarness(settings, "behavior.spawn_harness") ?? DEFAULT_HARNESS;
  const storedModel = readString(settings, "behavior.spawn_model");
  const storedEffort = readString(settings, "behavior.spawn_reasoning_effort");
  const [picks, setPicks] = useState<SpawnPicks>({});
  const selectedModel = picks.model ?? storedModel;
  const selectedReasoningEffort = picks.effort ?? storedEffort;
  const pickHarness = (v: Harness) => setPicks((p) => ({ ...p, harness: v }));
  const pickModel = (v: string) => setPicks((p) => ({ ...p, model: v }));
  const pickEffort = (v: string) => setPicks((p) => ({ ...p, effort: v }));

  // meiva:note
  // Ops reachability is not agent-host readiness. Keep the existing host-alive
  // verdict's meaning, and show non-spawnable hosts in the form with a reason.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const allMachines = statusData?.cluster.machines ?? [];
  const eligible = allMachines.filter(
    (m) => m.serve_agent_runner && m.online && m.paused === false,
  );
  const spawnable = eligible.filter((m) => m.agent_host_online === true);
  // meiva:note
  // The machine is the first column because it is the first fact: a harness is
  // only available *on* a machine, and one machine's probe says nothing about
  // another's. Every agent-runner machine is a row; a host that is down keeps its
  // row and is disabled with the reason, so the picker says what exists instead
  // of hiding it (user 2026-10-20).
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const machines: SpawnMachine[] = allMachines
    .filter((m) => m.serve_agent_runner && !m.paused)
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((m) => ({
      name: m.name,
      reason:
        !m.online
          ? t("machineOffline")
          : m.agent_host_online === true
            ? undefined
            : m.agent_host_online === false
              ? t("hostUnavailable")
              : t("hostUnknown"),
    }));
  // meiva:note
  // A pick survives a status refresh only while that machine is still offered;
  // otherwise the form falls back to the first machine whose host is up, so a
  // vanished host never leaves the form targeting a machine that is not on screen.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const pickedMachine =
    picks.machine !== undefined && machines.some((row) => row.name === picks.machine)
      ? picks.machine
      : undefined;
  const selectedMachine = pickedMachine ?? spawnable.at(0)?.name;
  const pickMachine = (v: string) => setPicks((p) => ({ ...p, machine: v }));
  const { snapshots: harnessByMachine, pending: pendingHarnesses } =
    useHarnessAvailabilityByMachine(machines.map((row) => row.name));
  const availabilityPending = statusQuery.isPending ||
    (selectedMachine !== undefined && pendingHarnesses.has(selectedMachine));
  // meiva:note
  // This machine's harnesses: a harness the machine cannot start keeps its row,
  // disabled and titled "unavailable", because an agent born on it would sit
  // inert while looking created. A stored or picked harness that this machine
  // cannot start falls through to the first one it can, so switching machine
  // never lands on a dead end - the pick itself is left alone, so switching back
  // restores it.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const tableHarnessRows = availabilityPending
    ? []
    : harnessRows(harnessByMachine.get(selectedMachine ?? "") ?? []);
  const pickedHarness = picks.harness ?? storedHarness;
  const selectedHarness: Harness =
    tableHarnessRows.find((row) => row.harness === pickedHarness)?.launchable
      ? pickedHarness
      : (tableHarnessRows.find((row) => row.launchable)?.harness ?? pickedHarness);
  const selectedHarnessRow = tableHarnessRows.find((row) => row.harness === selectedHarness);
  const harnessBlocked = !selectedHarnessRow?.launchable;

  // meiva:note
  // Native execution draws from `/api/models` and honors visibility/supersession
  // preferences; external harnesses draw their model roster, default model, and
  // per-model reasoning-effort ladders directly from the machine's live harness
  // probe snapshot, independent of native catalog membership or hiding.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const hiddenModels = readStringList(settings, "models.hidden");
  const hiddenProviders = readStringList(settings, "models.hidden_providers");
  const nativeModelGroups = groupedModels(
    modelsData,
    (m) =>
      !isSuperseded(modelsData, m) &&
      !hiddenModels.includes(m) &&
      !hiddenProviders.includes(modelsData?.models[m]?.provider ?? ""),
  );
  const probedModels = selectedHarnessRow?.models ?? [];
  const visibleModels =
    selectedHarness === DEFAULT_HARNESS
      ? nativeModelGroups.flatMap(([, models]) => models)
      : probedModels.map((item) => item.id);

  // meiva:note
  // The model the next agent will be born on: the stored choice while the chosen
  // harness can run it, else this harness's first available model (the stored
  // value stays, so switching the harness back restores it). Undefined only when
  // the active roster offers nothing for that harness.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const effectiveModel: string | undefined =
    selectedModel !== undefined && visibleModels.includes(selectedModel)
      ? selectedModel
      : visibleModels[0];

  const executionSettings =
    selectedHarness === DEFAULT_HARNESS
      ? (effectiveModel ? modelsData?.models[effectiveModel] : undefined)
      : (effectiveModel ? probedModels.find((item) => item.id === effectiveModel) : undefined);
  // meiva:note
  // The effort ladder this model offers, with all three "no ladder" shapes
  // collapsed into one empty list: the model carries no effort knob (wire null),
  // the models query has not resolved yet, and the resolved name is absent from
  // the catalog.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const effortLevels = executionSettings?.reasoning_effort_options ?? [];
  // meiva:note
  // The model's concrete default effort, published by GET /api/models from the
  // registry's per-model tuning (every catalog model pins one — validated
  // server-side). The table pre-selects it, so selecting a model immediately
  // marks the level that model runs at; there is no synthetic "Effort: default"
  // row.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const modelDefaultEffort =
    typeof executionSettings?.reasoning_effort_default === "string"
      ? executionSettings.reasoning_effort_default
      : null;
  // meiva:note
  // Re-derived from the effective model on every render rather than trusted from
  // the stored setting: a level the current model does not offer must not reach
  // the spawn request. Explicit selection wins, then the model's own default; a
  // model the catalog has not resolved publishes no default, and nothing is sent
  // for it. The stored setting is left as the user last set it, so switching back
  // to a model that offers it restores it.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
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
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
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
      // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
      ...(selectedHarness === DEFAULT_HARNESS ? {} : { harness: selectedHarness }),
    };
  }

  function spawnOn(machine: string) {
    rememberSpawnedConfig();
    onSpawn(buildOpts(machine));
    onDone();
  }

  // meiva:note
  // Keep the picker visible on a cold cache without treating pending data as a refusal.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  if (statusQuery.error && !statusData) {
    return (
      <InlineError
        role="status"
        className="px-3 py-2"
        onRetry={() => void statusQuery.refetch()}
        retryLabel={t("refresh")}
      >
        {t("statusUnavailable")}
      </InlineError>
    );
  }

  if (statusData && spawnable.length === 0) {
    const reason = eligible.length === 1
      ? eligible[0].agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")
      : t("noReadyRunner");
    return <div role="status" className="px-3 py-2 text-xs text-muted-foreground">{reason}</div>;
  }

  const target = machines.find((row) => row.name === selectedMachine);
  const refreshMachines = machines.filter((row) => row.reason === undefined).map((row) => row.name);
  const modelsBlocked =
    selectedHarness === DEFAULT_HARNESS ? Boolean(modelsQuery.error) : visibleModels.length === 0;
  const modelsPending = availabilityPending ||
    (selectedHarness === DEFAULT_HARNESS && modelsQuery.isPending);
  const spawnReason = availabilityPending ? undefined : target?.reason ?? (harnessBlocked
    ? t("harnessStatus.unavailable")
    : modelsBlocked ? t("modelsUnavailable") : undefined);
  const refreshNotice = refresh.isError
    ? t("refreshFailed")
    : !availabilityPending && !harnessByMachine.get(selectedMachine ?? "")?.length ? t("refreshNeeded") : undefined;
  // meiva:note
  // Rates are the decision only when the harness meters them. A subscription
  // harness pays nothing per token, so the price column stays empty there
  // (user 2026-10-20); the harness row is the authority on which it is.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
  const pickerProps = {
    machines,
    machine: selectedMachine,
    onMachine: pickMachine,
    harnessRows: tableHarnessRows,
    harness: selectedHarness,
    onHarness: pickHarness,
    modelIds: visibleModels,
    modelsPending,
    models: modelsData?.models,
    model: effectiveModel,
    onModel: pickModel,
    efforts: effortLevels,
    effort: effectiveReasoningEffort,
    onEffort: pickEffort,
    prices: tableHarnessRows.find((row) => row.harness === selectedHarness)?.billing === "api",
  };

  const spawnButton = (className?: string) => (
    <button
      type="button"
      disabled={modelsPending || spawnReason !== undefined || selectedMachine === undefined}
      title={spawnReason}
      onClick={() => { if (selectedMachine !== undefined) spawnOn(selectedMachine); }}
      className={cn(
        "items-center justify-center gap-1.5 rounded-sm border border-border px-2 py-1.5 text-xs font-medium hover:bg-sidebar-accent disabled:opacity-50",
        FLEX,
        className,
      )}
    >
      <Plus className="size-3.5" />
      <span>{t("spawn")}</span>
    </button>
  );

  const refreshButton = (className?: string) => (
    <button
      type="button"
      disabled={refreshing || refreshMachines.length === 0}
      onClick={() => {
        if (refreshMachines.length > 0) {
          refresh.mutate(refreshMachines, { onSettled: () => { void modelsQuery.refetch(); } });
        }
      }}
      className={cn(
        "items-center justify-center gap-1.5 rounded-sm border border-border px-2 py-1.5 text-xs hover:bg-sidebar-accent disabled:opacity-50",
        FLEX, className,
      )}
    >
      <RefreshCw className={cn("size-3.5 shrink-0", refreshing && "animate-spin")} />
      <span>{refreshing ? t("refreshing") : t("refresh")}</span>
    </button>
  );

  const notice = refreshNotice === undefined ? null : refresh.isError ? (
    <InlineError className="px-3 py-1.5">{refreshNotice}</InlineError>
  ) : (
    <p role="status" className="text-xs text-muted-foreground">{refreshNotice}</p>
  );

  const refusalElement = (className?: string) =>
    spawnReason !== undefined ? (
      modelsQuery.error ? (
        <InlineError className={cn("text-[11px] leading-tight", className)} data-testid="spawn-refusal">
          {spawnReason}
        </InlineError>
      ) : (
        <p data-testid="spawn-refusal" className={cn("text-xs text-muted-foreground", className)}>
          {spawnReason}
        </p>
      )
    ) : null;

  if (layout === "stack") {
    return (
      // meiva:note
      // The narrow arrangement: the four decisions stack, the action sits under
      // them at full width, and a refusal is written out instead of titled — a
      // phone has no hover for a tooltip (2026-10-21).
      // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
      <div className={cn("rounded-md border border-border bg-popover text-popover-foreground", FLEX, FLEX_COL)}>
        <SpawnStack {...pickerProps} />
        {refusalElement("px-3 py-2")}
        <div className={cn("gap-2 border-t border-border p-2", FLEX, FLEX_COL)}>
          {notice}
          {refreshButton("w-full min-h-11 text-sm")}
          {spawnButton("w-full min-h-11 text-sm")}
        </div>
      </div>
    );
  }

  return (
    // meiva:note
    // The action sits to the right of the table, not under it: a footer row made
    // the popover read as a narrow, tall strip (user 2026-10-20). The rail is a
    // fixed-width column, so the table keeps its own width and the button lines up
    // with the columns it acts on.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    <div className={cn("rounded-md border border-border bg-popover text-popover-foreground", FLEX)}>
      <div className={cn(MIN_W_0, FLEX_1)}>
        <SpawnTable {...pickerProps} />
      </div>
      <div className={cn("w-[7rem] shrink-0 justify-end gap-2 border-l border-border p-2", FLEX, FLEX_COL)}>
        {refusalElement()}
        {notice}
        {refreshButton("w-full")}
        {spawnButton("w-full")}
      </div>
    </div>
  );
}
