"use client";

import { writeConfigOwners } from "./_owner_writes";

// meiva:link ui/web/src/app/control/config/docs/configuration.meiva.okf.md

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, RefreshCw, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { DeferredMount } from "@/components/app/deferred-mount";
import type { BrowserStorage } from "@/lib/browser/browser";
import { useBrowser } from "@/lib/browser/browser-provider";

import { errMsg } from "@/lib/contracts/errors";
import type {
  AgentMachineRow,
  ConfigFieldView,
  ConfigView,
  ConfigWriteResult,
} from "@/lib/contracts/types";
import { SYSTEM_STATUS_QUERY_KEY } from "@/lib/notifications/use-cluster-health";

import {
  DEFAULT_MODEL_GROUP_ID,
  PER_MODEL_GROUP_ID,
} from "../config-group-defs";
import { displayGroupId } from "../config-groups";
import { useSectionVisible } from "../visibility";
import {
  ConfigFilterBar,
  ConfigRow,
  MachineSelector,
  buildConfigGroupSections,
  isFieldEditable,
  restartLabel,
  type MachineSelection,
} from "./_config_row";
import { DefaultModelPanel } from "./_default_model";
import { PerModelPanel } from "./_per_model";
import { InlineError } from "@/components/ui/inline-error";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { useApi } from "@/lib/transport/transport-context";

const COMMON_FIELDS = new Set([
  "MEIVA_AGENT_COMMUNICATION_STYLE", "MEIVA_REASONING_EFFORT",
]);

const configQueryKey = (machine: MachineSelection) => ["config", machine] as const;
const MACHINES_QUERY_KEY = ["machines"] as const;
const COLLAPSED_GROUPS_STORAGE_KEY = "control.config.collapsedGroups";

function applyDelta(
  prev: Record<string, unknown>,
  name: string,
  value: unknown,
): Record<string, unknown> {
  const next = { ...prev };
  next[name] = value;
  return next;
}

function hydrateCollapsedGroups(
  storage: BrowserStorage,
  apply: (state: { groups: Set<string>; hydrated: boolean }) => void,
) {
  let groups = new Set<string>();
  const stored = storage.read(COLLAPSED_GROUPS_STORAGE_KEY);
  if (stored.state === "found") {
    try {
      const parsed: unknown = JSON.parse(stored.value);
      if (Array.isArray(parsed)) {
        groups = new Set(parsed.filter((id): id is string => typeof id === "string"));
      }
    } catch {
      groups = new Set();
    }
  }
  apply({ groups, hydrated: true });
}

export default function ConfigEditor({ mode }: { mode: "general" | "advanced" | "machines" }) {
  const client = useApi();
  const t = useTranslations("config");
  const browser = useBrowser();
  const view = browser.view;
  const qc = useQueryClient();
  const visible = useSectionVisible();
  const [selection, setSelection] = useState<MachineSelection>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<string>("all");
  const [collapsedState, setCollapsedState] = useState<{
    groups: Set<string>;
    hydrated: boolean;
  }>({ groups: new Set(), hydrated: false });

  useEffect(() => {
    hydrateCollapsedGroups(browser.local, setCollapsedState);
  }, [browser.local]);

  useEffect(() => {
    if (!collapsedState.hydrated) return;
    // meiva:note
    // The port answers `unavailable` for a browser that refuses storage; the selection then
    // simply stops being remembered for the next visit, which is what the catch used to do.
    browser.local.write(COLLAPSED_GROUPS_STORAGE_KEY, JSON.stringify([...collapsedState.groups]));
  }, [browser.local, collapsedState]);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: configQueryKey(selection),
    queryFn: () => client.getConfig(selection ?? undefined),
    enabled: visible,
  });

  const machines = useQuery({
    queryKey: MACHINES_QUERY_KEY,
    queryFn: () => client.getMachines(),
    enabled: visible && mode === "machines",
  });
  const status = useQuery({
    queryKey: SYSTEM_STATUS_QUERY_KEY,
    queryFn: () => client.getSystemStatus(),
    enabled: visible && mode === "machines",
  });
  const gatewayName = status.data?.cluster.current_machine ?? null;

  const [saveError, setSaveError] = useState<string | null>(null);
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // meiva:note
  // The last write reports which processes need restarting; unrelated rows
  // with restart requirements must not contribute to this result banner.
  const [restartTargets, setRestartTargets] = useState<string[]>([]);
  // meiva:note
  // The field the per-model view sent us to; its row keeps a ring until another
  // link is followed, so a long group list doesn't swallow the landing spot.
  const [focusedField, setFocusedField] = useState<string | null>(null);

  // meiva:note
  // A write reported per-field verdicts. Mark any !ok field with its reason;
  // the query invalidate (onSettled) restores authoritative pre-write values
  // (each owner reports its own successful or rejected write).
  const applyWriteResult = useCallback((names: string[], result: ConfigWriteResult) => {
    const failures: Record<string, string> = {};
    for (const name of new Set([...names, ...Object.keys(result.results)])) {
      const r = Object.hasOwn(result.results, name) ? result.results[name] : undefined;
      if (r?.ok === false) failures[name] = r.reason ?? "rejected";
    }
    const written = new Set([...names, ...Object.keys(result.results)]);
    setFieldErrors((prev) => ({
      ...Object.fromEntries(Object.entries(prev).filter(([k]) => !written.has(k))),
      ...failures,
    }));
    setRestartTargets(Object.values(result.results).some((field) => field.ok) ? result.restart_required : []);
    setSaveError(result.applied ? null : `Save failed for: ${Object.keys(failures).join(", ")}`);
  }, []);

  // meiva:note
  // No optimistic writes. The toggle / input shows a spinner while the PUT
  // is in flight; on settle we invalidate so the next render shows
  // authoritative state — which also reverts a server-rejected field.
  //
  // `machine` is included in the mutation variables so onSettled can
  // invalidate the exact query key that was written — even if the user
  // switches machines while the PUT is in-flight.
  const toggleBool = useMutation({
    mutationFn: async ({
      name,
      value,
      machine,
    }: {
      name: string;
      value: boolean;
      machine: MachineSelection;
    }) => {
      const current = qc.getQueryData<ConfigView>(configQueryKey(machine));
      if (!current) throw new Error("config data not loaded");
      const result = await writeConfigOwners(
        applyDelta(current.raw_overrides, name, value), current.fields,
        (patch) => client.putConfig(patch, machine ?? undefined),
      );
      return { name, result };
    },
    onSuccess: ({ name, result }) => applyWriteResult([name], result),
    onError: (err: unknown) => {
      setSaveError(`Save failed: ${errMsg(err)}`);
    },
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: configQueryKey(variables.machine) });
    },
  });

  const saveText = useMutation({
    mutationFn: async ({
      name,
      value,
      machine,
    }: {
      name: string;
      value: string;
      machine: MachineSelection;
    }) => {
      const current = qc.getQueryData<ConfigView>(configQueryKey(machine));
      if (!current) throw new Error("config data not loaded");
      const field = current.fields.find((f) => f.name === name);
      if (!field) throw new Error(`unknown field: ${name}`);
      let parsed: unknown = value;
      if (field.field_type === "int") parsed = parseInt(value, 10);
      else if (field.field_type === "float") parsed = parseFloat(value);
      else if (field.name === "agent_communication_style" && value === "") parsed = null;
      const result = await writeConfigOwners(
        applyDelta(current.raw_overrides, name, parsed), current.fields,
        (patch) => client.putConfig(patch, machine ?? undefined),
      );
      return { name, result };
    },
    onMutate: () => {
      setEditingField(null);
    },
    onSuccess: ({ name, result }) => applyWriteResult([name], result),
    onError: (err: unknown) => {
      setSaveError(`Save failed: ${errMsg(err)}`);
    },
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: configQueryKey(variables.machine) });
    },
  });

  // meiva:note
  // A per-model row links to the field's editor down in the group list. The
  // field may be filtered out, so reset the filter first; the scroll runs in an
  // effect, i.e. after that re-render has put the row back in the DOM.
  const focusField = useCallback((name: string) => {
    setFilter("all");
    setSearch("");
    setFocusedField(name);
    const field = data?.fields.find((candidate) => candidate.name === name);
    if (field) {
      const groupId = displayGroupId(field);
      setCollapsedState((prev) => {
        if (!prev.groups.has(groupId)) return prev;
        const groups = new Set(prev.groups);
        groups.delete(groupId);
        return { ...prev, groups };
      });
    }
  }, [data]);
  useEffect(() => {
    if (!focusedField) return;
    const field = document.getElementById(`config-field-${focusedField}`);
    if (field) view.scrollIntoView(field, "center");
  }, [focusedField, view]);

  const onSelectMachine = useCallback((next: MachineSelection) => {
    setSelection(next);
    setSaveError(null);
    setEditingField(null);
    setFieldErrors({});
    setRestartTargets([]);
  }, []);

  const startEdit = useCallback((field: ConfigFieldView) => {
    setEditingField(field.name);
    setEditValue(field.sensitive ? "" : String(field.current_value ?? ""));
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingField(null);
    setEditValue("");
  }, []);

  const commitEdit = useCallback(
    (field: ConfigFieldView) => {
      if (field.sensitive) {
        if (editValue === "") {
          cancelEdit();
          return;
        }
        saveText.mutate({ name: field.name, value: editValue, machine: selection });
        return;
      }
      if (editValue === String(field.current_value ?? "")) {
        cancelEdit();
        return;
      }
      saveText.mutate({ name: field.name, value: editValue, machine: selection });
    },
    [editValue, cancelEdit, saveText, selection],
  );

  const onlineAgentRunners = machines.data
    ? machines.data.filter((m: AgentMachineRow) => m.live && m.name !== gatewayName)
    : [];

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">{t("loading")}</p>;
  }
  // meiva:note
  // stale-while-error (Task #1051): a failed post-write refetch (e.g. the
  // gateway restarting) must not replace the config the user is editing
  // with an error line — only a cold failure shows the error state.
  if (error && !data) {
    return (
      <InlineError onRetry={() => void refetch()}>
        {t("couldntLoad")}
      </InlineError>
    );
  }
  if (!data) return <p className="text-sm text-muted-foreground">(no data)</p>;

  const isRemote = selection !== null;
  const allGroups = buildConfigGroupSections(
    data.fields.filter((field) => {
      if ((field.scope === "host") !== (mode === "machines")) return false;
      if (mode === "general" && !COMMON_FIELDS.has(field.env_var)) return false;
      return `${field.label} ${field.env_var} ${field.description}`.toLowerCase().includes(search.toLowerCase());
    }),
    data.machine_capabilities,
    selection,
    filter,
  );
  const generalFields = allGroups.flatMap((group) => group.fields);
  const groupSections = mode === "general" ? [
    { id: "config-communication", label: t("workingTogether"), fields: generalFields.filter((field) => field.name === "agent_communication_style") },
    { id: "config-model-preferences", label: t("modelsHeading"), fields: generalFields.filter((field) => field.name !== "agent_communication_style") },
  ] : allGroups;
  const shownCount = groupSections.reduce((n, g) => n + g.fields.length, 0);

  return (
    <div className="space-y-4">
      {mode === "machines" && (
        <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-2">
          <MachineSelector selection={selection} agentRunners={onlineAgentRunners}
            gatewayName={gatewayName} onChange={onSelectMachine} />
          <p className="text-xs text-muted-foreground">{t("machineScope")}</p>
        </div>
      )}
      {mode !== "general" && (
        <div className="space-y-3">
          <input type="search" aria-label={t("searchSettings")} placeholder={t("searchSettings")}
            value={search} onChange={(event) => setSearch(event.target.value)}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">{t("technicalFilters")}</summary>
            <ConfigFilterBar filter={filter} shownCount={shownCount} onSelectFilter={setFilter} />
          </details>
        </div>
      )}
      {mode === "advanced" && (
        <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer text-sm font-medium">{t("perModelValues")}</summary>
          <div className="pt-4"><PerModelPanel id={PER_MODEL_GROUP_ID} onFocusField={focusField} /></div>
        </details>
      )}
      {mode !== "general" && shownCount === 0 && <p className="text-sm text-muted-foreground">{t("noMatches")}</p>}

      {groupSections.map(({ id, label, fields }) => {
        const collapsed = mode !== "general" && collapsedState.groups.has(id);
        const fieldsId = `${id}-fields`;
        return (
          <div key={id} id={id} className="scroll-mt-4" data-testid={`config-group-${id}`}>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
              {mode === "general" ? <span className="px-1">{label}</span> : <button
                type="button"
                aria-expanded={!collapsed}
                aria-controls={fieldsId}
                onClick={() =>
                  setCollapsedState((prev) => {
                    const groups = new Set(prev.groups);
                    if (collapsed) groups.delete(id);
                    else groups.add(id);
                    return { ...prev, groups };
                  })
                }
                className={cn("w-full items-center justify-between rounded px-1 py-0.5 text-left hover:bg-muted/50", FLEX)}
              >
                <span>
                  {label}{" "}
                  <span className="ml-1 font-normal text-muted-foreground/70">({fields.length})</span>
                </span>
                <ChevronDown
                  className={cn("size-4 shrink-0 transition-transform", collapsed && "-rotate-90")}
                  aria-hidden
                />
              </button>}
            </h3>
            {mode === "general" && id === "config-model-preferences" && (
              <div className="rounded-md border border-border p-4 mb-3">
                <DefaultModelPanel id={DEFAULT_MODEL_GROUP_ID} />
              </div>
            )}
            {!collapsed && fields.length > 0 ? (
              <DeferredMount>
                <div id={fieldsId} className="border border-border rounded-md divide-y divide-border">
                  {fields.map((field) => (
                    <ConfigRow
                      key={field.name}
                      field={field}
                      simple={mode === "general"}
                      editable={isFieldEditable(field, selection)}
                      highlighted={focusedField === field.name}
                      remoteReadOnly={isRemote && field.scope !== "host"}
                      fieldError={fieldErrors[field.name] ?? null}
                      editingField={editingField}
                      editValue={editValue}
                      busy={toggleBool.isPending || saveText.isPending}
                      onToggle={(v) => {
                        setFieldErrors((prev) =>
                          Object.fromEntries(
                            Object.entries(prev).filter(([k]) => k !== field.name),
                          ),
                        );
                        toggleBool.mutate({ name: field.name, value: v, machine: selection });
                      }}
                      onSelectEnum={(v) => {
                        setFieldErrors((prev) =>
                          Object.fromEntries(
                            Object.entries(prev).filter(([k]) => k !== field.name),
                          ),
                        );
                        saveText.mutate({ name: field.name, value: v, machine: selection });
                      }}
                      onStartEdit={() => startEdit(field)}
                      onCancelEdit={cancelEdit}
                      onEditValueChange={setEditValue}
                      onCommitEdit={() => commitEdit(field)}
                      onEditKeyDown={(e) => {
                        if (e.key === "Enter") commitEdit(field);
                        else if (e.key === "Escape") cancelEdit();
                      }}
                    />
                  ))}
                </div>
              </DeferredMount>
            ) : null}
          </div>
        );
      })}

      {saveError && (
        <InlineError>{saveError}</InlineError>
      )}

      {restartTargets.length > 0 && (
        <div role="status" className={cn("fixed bottom-6 right-6 z-50 max-w-[min(28rem,calc(100vw-3rem))] items-start gap-3 rounded-xl border border-border bg-popover p-4 text-sm text-popover-foreground shadow-lg", FLEX)}>
          <RefreshCw aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <span>{t("restartAfterSave", { targets: restartTargets.map((target) => restartLabel(target, t)).join(", ") })}</span>
          <button type="button" aria-label={t("dismissReminder")} onClick={() => setRestartTargets([])}
            className="shrink-0 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
            <X aria-hidden className="size-4" />
          </button>
        </div>
      )}
    </div>
  );
}
