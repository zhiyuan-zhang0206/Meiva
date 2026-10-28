"use client";

import { writeConfigOwners } from "./_owner_writes";

// meiva:note
// /control#config — runtime config management, per-machine.
//
// Selector: a machine dropdown sits above the group sections. Default is
// "Cluster (gateway)" — that view edits cluster-scope fields + this
// host's own host fields. Selecting an online agent-runner switches to that
// host's view: its host fields become the editable ones, while the
// cluster / agent fields render read-only (they're machine-independent;
// edit them from the Cluster view). A remote view also drops fields whose
// capability the machine doesn't carry (a pure agent-runner hides
// gateway-capability fields — they're edited on the Cluster view).
//
// Read: GET /api/config[?machine=] returns ConfigView (field metadata +
// raw_overrides). Write: PUT /api/config[?machine=] merges a patch into the
// override layer (reducer semantics: a value sets, null unsets, an absent key
// is left untouched) and returns a ConfigWriteResult (per-field ok/reason +
// restart_required).
//
// Layout: a sticky tag-filter bar, then the 17 semantic display groups from
// config-groups.ts (a FRONTEND regrouping of the backend's domain groups —
// each group element id is a nav sub-anchor). Every row shows a sentence-case
// label derived from the env var (the env var itself rides in the title
// attribute) plus small tag badges: Runtime / CLI-only (effective
// editability), the raw scope, per-agent, "Startup: <process>".
//
// Interactions:
// - bool fields → ON/OFF toggle, click PUTs immediately
// - string/int/float fields → click value to edit, Enter / blur to save
// - CLI-only (non-editable) fields → dimmed row, read-only value
// - sensitive fields (API keys) → write-only replace: never read back (masked
//   ••••••• + lock icon), but if the field is writable a "Replace" pencil opens
//   an empty password input. Submitting a non-empty value replaces the secret;
//   an empty submit is a no-op (keeps the current value). The backend round-trips
//   the unchanged-sentinel for every OTHER secret, so replacing one key never
//   touches another. A non-writable secret (db_url / redis_url) stays read-only.
// - a host bool field with can_enable === false → pre-greyed disabled, with
//   its reason shown inline (e.g. "no display detected")
//
// Write result: the PUT is atomic server-side. If a field's result is
// !ok, its reason is shown inline (red) and the displayed value reverts
// (the refetch restores authoritative pre-write state). restart_required
// from the result drives the per-machine restart banner.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { DeferredMount } from "@/components/app/deferred-mount";
import { useBrowser } from "@/lib/browser/browser-provider";
import { api } from "@/lib/transport/api";
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
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

const configQueryKey = (machine: MachineSelection) => ["config", machine] as const;
const MACHINES_QUERY_KEY = ["machines"] as const;
const COLLAPSED_GROUPS_STORAGE_KEY = "control.config.collapsedGroups";

/** Apply a single-field delta on top of raw_overrides, return a new overrides object. */
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
  apply: (state: { groups: Set<string>; hydrated: boolean }) => void,
) {
  let groups = new Set<string>();
  try {
    const stored = localStorage.getItem(COLLAPSED_GROUPS_STORAGE_KEY);
    if (stored != null) {
      const parsed: unknown = JSON.parse(stored);
      if (Array.isArray(parsed)) {
        groups = new Set(parsed.filter((id): id is string => typeof id === "string"));
      }
    }
  } catch {
    groups = new Set();
  }
  apply({ groups, hydrated: true });
}

export default function ConfigPage() {
  const t = useTranslations("config");
  const view = useBrowser().view;
  const qc = useQueryClient();
  const visible = useSectionVisible();
  const [selection, setSelection] = useState<MachineSelection>(null);
  const [filter, setFilter] = useState<string>("all");
  const [collapsedState, setCollapsedState] = useState<{
    groups: Set<string>;
    hydrated: boolean;
  }>({ groups: new Set(), hydrated: false });

  useEffect(() => {
    hydrateCollapsedGroups(setCollapsedState);
  }, []);

  useEffect(() => {
    if (!collapsedState.hydrated) return;
    try {
      localStorage.setItem(
        COLLAPSED_GROUPS_STORAGE_KEY,
        JSON.stringify([...collapsedState.groups]),
      );
    } catch {
      return;
    }
  }, [collapsedState]);

  const { data, isLoading, error } = useQuery({
    queryKey: configQueryKey(selection),
    queryFn: () => api.getConfig(selection ?? undefined),
    enabled: visible,
  });

  // Machine list (name + live) drives the selector's agent-runner options.
  const machines = useQuery({
    queryKey: MACHINES_QUERY_KEY,
    queryFn: () => api.getMachines(),
    enabled: visible,
  });
  // Gateway machine name — labels the Cluster option (which machine owns the
  // cluster config). Machines list uniformly by role: a single box that carries
  // both capabilities appears as both the Cluster option and an agent-runner row,
  // with no single-box special-casing.
  const status = useQuery({
    queryKey: SYSTEM_STATUS_QUERY_KEY,
    queryFn: () => api.getSystemStatus(),
    enabled: visible,
  });
  const gatewayName = status.data?.cluster.current_machine ?? null;

  const [saveError, setSaveError] = useState<string | null>(null);
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>("");
  // Per-field write rejections (red inline note), keyed by field name. Set
  // from a ConfigWriteResult; cleared when a new write for that field starts.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // Restart targets reported by the last successful write — drives the
  // per-machine banner instead of the always-on field-scan derivation.
  const [restartTargets, setRestartTargets] = useState<string[]>([]);
  // The field the per-model view sent us to; its row keeps a ring until another
  // link is followed, so a long group list doesn't swallow the landing spot.
  const [focusedField, setFocusedField] = useState<string | null>(null);

  // A write reported per-field verdicts. Mark any !ok field with its reason;
  // the query invalidate (onSettled) restores authoritative pre-write values
  // (each owner reports its own successful or rejected write).
  const applyWriteResult = useCallback((names: string[], result: ConfigWriteResult) => {
    const failures: Record<string, string> = {};
    for (const name of new Set([...names, ...Object.keys(result.results)])) {
      const r = result.results[name] as
        | ConfigWriteResult["results"][string]
        | undefined;
      if (r?.ok === false) failures[name] = r.reason ?? "rejected";
    }
    const written = new Set([...names, ...Object.keys(result.results)]);
    setFieldErrors((prev) => ({
      ...Object.fromEntries(Object.entries(prev).filter(([k]) => !written.has(k))),
      ...failures,
    }));
    setRestartTargets(result.restart_required);
    setSaveError(result.applied ? null : `Save failed for: ${Object.keys(failures).join(", ")}`);
  }, []);

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
        (patch) => api.putConfig(patch, machine ?? undefined),
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
      // Type conversion
      let parsed: unknown = value;
      if (field.field_type === "int") parsed = parseInt(value, 10);
      else if (field.field_type === "float") parsed = parseFloat(value);
      const result = await writeConfigOwners(
        applyDelta(current.raw_overrides, name, parsed), current.fields,
        (patch) => api.putConfig(patch, machine ?? undefined),
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

  // A per-model row links to the field's editor down in the group list. The
  // field may be filtered out, so reset the filter first; the scroll runs in an
  // effect, i.e. after that re-render has put the row back in the DOM.
  const focusField = useCallback((name: string) => {
    setFilter("all");
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
    ? machines.data.filter((m: AgentMachineRow) => m.live)
    : [];

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">{t("loading")}</p>;
  }
  // stale-while-error (Task #1051): a failed post-write refetch (e.g. the
  // gateway restarting) must not replace the config the user is editing
  // with an error line — only a cold failure shows the error state.
  if (error && !data) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("couldntLoad")}
      </p>
    );
  }
  if (!data) return <p className="text-sm text-muted-foreground">(no data)</p>;

  // On a remote (agent-runner) view, non-host fields are machine-independent
  // and cannot be edited here.
  const isRemote = selection !== null;
  const groupSections = buildConfigGroupSections(
    data.fields,
    data.machine_capabilities,
    selection,
    filter,
  );
  const shownCount = groupSections.reduce((n, g) => n + g.fields.length, 0);

  return (
    <div className="space-y-4">
      <MachineSelector
        selection={selection}
        agentRunners={onlineAgentRunners}
        gatewayName={gatewayName}
        onChange={onSelectMachine}
      />

      {/* The cluster's default model — a DB row, not a `.env` field, so it sits
          above the field list rather than inside a group. */}
      <DefaultModelPanel id={DEFAULT_MODEL_GROUP_ID} />

      {/* Read-only resolution view. Above the filter bar because it answers a
          different question than the rows below ("what does model X get")
          and its own rows link down into them. */}
      <PerModelPanel id={PER_MODEL_GROUP_ID} onFocusField={focusField} />

      {/* Sticky tag-filter bar — stays pinned while the long field list scrolls. */}
      <ConfigFilterBar filter={filter} shownCount={shownCount} onSelectFilter={setFilter} />

      {isRemote && (
        <p className="text-[11px] italic text-muted-foreground">
          Editing {selection}&apos;s host fields; cluster fields are read-only here — edit
          them on the Cluster view.
        </p>
      )}

      {groupSections.map(({ id, label, fields }) => {
        const collapsed = collapsedState.groups.has(id);
        const fieldsId = `${id}-fields`;
        return (
          <div key={id} id={id} className="scroll-mt-4" data-testid={`config-group-${id}`}>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
              <button
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
              </button>
            </h3>
            {!collapsed ? (
              <DeferredMount>
                <div id={fieldsId} className="border border-border rounded-md divide-y divide-border">
                  {fields.map((field) => (
                    <ConfigRow
                      key={field.name}
                      field={field}
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
        <p className="text-sm text-destructive">{saveError}</p>
      )}

      {restartTargets.length > 0 && (
        <div className={cn("items-start gap-2 text-xs text-muted-foreground border-t border-border pt-3", FLEX)}>
          <RefreshCw className="size-3.5 mt-0.5 shrink-0" />
          <span>
            {selection ?? "Cluster"}: restart the following processes for changes to take
            effect: {restartTargets.map((x) => restartLabel(x, t)).join(", ")}
          </span>
        </div>
      )}
    </div>
  );
}
