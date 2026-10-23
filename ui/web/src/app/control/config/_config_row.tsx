"use client";

import { Check, Lock, Pencil, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type { AgentMachineRow, ConfigFieldView } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";

import {
  CONFIG_DISPLAY_GROUPS,
  CONFIG_FILTERS,
  HIDDEN_ENV_VARS,
} from "../config-group-defs";
import {
  displayGroupId,
  displayOrder,
  fieldFilterTags,
  fieldLabel,
  formatConfigValue,
  isPerAgent,
} from "../config-groups";

export type MachineSelection = string | null;

function focusInputOnMount(el: HTMLInputElement | null) {
  el?.focus();
}

export function restartLabel(
  target: string,
  t: ReturnType<typeof useTranslations<"config">>,
): string {
  switch (target) {
    case "agent":
      return t("agentProcess");
    case "ops":
      return t("opsDaemon");
    case "gateway":
      return t("gatewayProcess");
    case "all":
      return t("allProcesses");
    default:
      return target;
  }
}

export function isFieldEditable(field: ConfigFieldView, selection: MachineSelection): boolean {
  if (selection === null) return field.writable;
  return field.scope === "host" && field.remote_writable;
}

export function buildConfigGroupSections(
  fields: readonly ConfigFieldView[],
  machineCapabilities: readonly string[],
  selection: MachineSelection,
  filter: string,
): { id: string; label: string; fields: ConfigFieldView[] }[] {
  const isRemote = selection !== null;
  const shownFields = fields.filter(
    (f) =>
      !HIDDEN_ENV_VARS.has(f.env_var) &&
      (!isRemote || f.capability === "common" || machineCapabilities.includes(f.capability)),
  );

  const buckets = new Map<string, ConfigFieldView[]>();
  for (const field of shownFields) {
    const gid = displayGroupId(field);
    const bucket = buckets.get(gid);
    if (bucket) bucket.push(field);
    else buckets.set(gid, [field]);
  }
  for (const bucket of buckets.values()) {
    bucket.sort(
      (a, b) => displayOrder(a) - displayOrder(b) || a.env_var.localeCompare(b.env_var),
    );
  }

  const matchesFilter = (field: ConfigFieldView) =>
    filter === "all" ||
    fieldFilterTags(field, isFieldEditable(field, selection)).has(filter);

  return CONFIG_DISPLAY_GROUPS.map(({ id, label }) => {
    const all = buckets.get(id) ?? [];
    return { id, label, fields: all.filter(matchesFilter) };
  }).filter((g) => g.fields.length > 0);
}

export function MachineSelector({
  selection,
  agentRunners,
  gatewayName,
  onChange,
}: {
  selection: MachineSelection;
  agentRunners: AgentMachineRow[];
  gatewayName: string | null;
  onChange: (next: MachineSelection) => void;
}) {
  const t = useTranslations("config");
  const clusterLabel = gatewayName
    ? t("clusterGateway", { name: gatewayName })
    : t("clusterGatewayPlain");
  return (
    <div className={cn("items-center gap-2", FLEX)}>
      <label htmlFor="config-machine" className="text-xs text-muted-foreground">
        {t("machine")}
      </label>
      <select
        id="config-machine"
        aria-label={t("machine")}
        value={selection ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
        className="text-xs bg-transparent border border-border rounded px-1.5 py-1 text-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
      >
        <option value="">{clusterLabel}</option>
        {agentRunners.map((m) => (
          <option key={m.name} value={m.name}>
            {m.name}
          </option>
        ))}
      </select>
    </div>
  );
}

function TagBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded border border-border px-1 text-[11px] text-muted-foreground">
      {children}
    </span>
  );
}

export function ConfigRow({
  field,
  editable,
  highlighted,
  remoteReadOnly,
  fieldError,
  editingField,
  editValue,
  busy,
  onToggle,
  onSelectEnum,
  onStartEdit,
  onCancelEdit,
  onEditValueChange,
  onCommitEdit,
  onEditKeyDown,
}: {
  field: ConfigFieldView;
  editable: boolean;
  highlighted: boolean;
  remoteReadOnly: boolean;
  fieldError: string | null;
  editingField: string | null;
  editValue: string;
  busy: boolean;
  onToggle: (v: boolean) => void;
  onSelectEnum: (v: string) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onEditValueChange: (v: string) => void;
  onCommitEdit: () => void;
  onEditKeyDown: (e: React.KeyboardEvent) => void;
}) {
  const t = useTranslations("config");
  const isEditing = editingField === field.name;
  const displayValue = displayFieldValue(field);
  const defaultText = formatConfigValue(field.default_value);
  const capabilityBlocked = field.field_type === "bool" && field.can_enable === false;
  const toggleDisabled = busy || capabilityBlocked;

  return (
    <div
      id={`config-field-${field.name}`}
      className={`flex items-start justify-between gap-4 px-3 py-2 scroll-mt-12 ${
        editable ? "" : "bg-muted/40"
      } ${highlighted ? "ring-1 ring-primary ring-inset" : ""}`}
    >
      <div className={cn("space-y-0.5", FLEX_1, MIN_W_0)}>
        <div className={cn("items-center gap-2 flex-wrap", FLEX)}>
          <span className="font-medium break-all" title={field.env_var}>
            {fieldLabel(field.env_var)}
          </span>
          {field.writable ? (
            <TagBadge>{t("runtime")}</TagBadge>
          ) : (
            <span className="rounded border border-border bg-muted px-1 text-[11px] font-medium">
              {t("cliOnly")}
            </span>
          )}
          <TagBadge>{field.scope}</TagBadge>
          {isPerAgent(field) && <TagBadge>{t("perAgent")}</TagBadge>}
          {field.restart_required && <TagBadge>Startup: {field.restart_required}</TagBadge>}
          {remoteReadOnly && (
            <span className="text-[11px] text-muted-foreground italic">
              {t("editOnCluster")}
            </span>
          )}
          {!editable && field.writable && !remoteReadOnly && (
            <span className="text-[11px] text-muted-foreground italic">
              {t("readOnly")}
            </span>
          )}
        </div>
        {field.description && (
          <p className="text-xs text-muted-foreground leading-snug">
            {field.description}
          </p>
        )}
        {editable && capabilityBlocked && field.reason && (
          <p className="text-[11px] text-muted-foreground italic" data-testid={`capability-${field.name}`}>
            {field.reason}
          </p>
        )}
        {fieldError && (
          <p className="text-[11px] text-destructive" data-testid={`error-${field.name}`}>
            {fieldError}
          </p>
        )}
      </div>

      <div className={cn("shrink-0 items-end gap-1", FLEX, FLEX_COL)}>
        <div className={cn("items-center gap-1", FLEX)}>
        {field.sensitive ? (
          isEditing ? (
            <>
              <Input
                ref={focusInputOnMount}
                type="password"
                value={editValue}
                aria-label={t("replace", { env: field.env_var })}
                placeholder={t("newValue")}
                onChange={(e) => onEditValueChange(e.target.value)}
                onKeyDown={onEditKeyDown}
                className="h-7 w-48 text-xs font-mono"
                data-testid={`input-${field.name}`}
              />
              <Button
                variant="ghost"
                size="sm"
                onClick={onCommitEdit}
                disabled={busy}
                className="h-7 px-1"
                data-testid={`save-${field.name}`}
              >
                <Check className="size-3" />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onCancelEdit}
                className="h-7 px-1"
              >
                <X className="size-3" />
              </Button>
            </>
          ) : (
            <>
              <span
                className={cn(
                  "items-center gap-1 px-1 text-xs text-muted-foreground",
                  editable && "cursor-text rounded hover:bg-muted/50",
                  FLEX,
                )}
                data-testid={`sensitive-${field.name}`}
              >
                <Lock className="size-3" aria-hidden />
                {field.current_value ? "••••••••" : "(not set)"}
              </span>
              {editable && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onStartEdit}
                  disabled={busy}
                  className="h-7 px-1"
                  aria-label={`Replace ${field.env_var}`}
                  data-testid={`edit-${field.name}`}
                >
                  <Pencil className="size-3" />
                </Button>
              )}
            </>
          )
        ) : field.field_type === "bool" ? (
          <Switch
            checked={!!field.current_value}
            disabled={!editable || toggleDisabled}
            data-testid={`toggle-${field.name}`}
            aria-label={field.env_var}
            onCheckedChange={(v) => onToggle(v)}
          />
        ) : field.field_type === "enum" ? (
          editable ? (
            <select
              value={String(field.current_value ?? "")}
              disabled={busy}
              data-testid={`select-${field.name}`}
              onChange={(e) => onSelectEnum(e.target.value)}
              className="text-xs bg-transparent border border-border rounded px-1.5 py-1 text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer disabled:opacity-50"
            >
              {(field.current_value == null || field.current_value === "") && (
                <option value="" disabled>
                  {t("notSet")}
                </option>
              )}
              {(field.choices ?? []).map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-xs text-muted-foreground px-2">
              {displayValue}
            </span>
          )
        ) : isEditing ? (
          <>
            <Input
              ref={focusInputOnMount}
              type={field.field_type === "int" || field.field_type === "float" ? "number" : "text"}
              value={editValue}
              onChange={(e) => onEditValueChange(e.target.value)}
              onKeyDown={onEditKeyDown}
              className="h-7 w-48 text-xs font-mono"
              data-testid={`input-${field.name}`}
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={onCommitEdit}
              disabled={busy}
              className="h-7 px-1"
              data-testid={`save-${field.name}`}
            >
              <Check className="size-3" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={onCancelEdit}
              className="h-7 px-1"
            >
              <X className="size-3" />
            </Button>
          </>
        ) : (
          <>
            <span
              className={cn(
                "block max-w-48 truncate px-1 text-xs",
                editable
                  ? "cursor-text rounded hover:bg-muted/50"
                  : "text-muted-foreground",
              )}
              title={displayValue}
            >
              {displayValue}
            </span>
            {editable && (
              <Button
                variant="ghost"
                size="sm"
                onClick={onStartEdit}
                disabled={busy}
                className="h-7 px-1"
                data-testid={`edit-${field.name}`}
              >
                <Pencil className="size-3" />
              </Button>
            )}
          </>
        )}
        </div>
        <span className="text-[11px] text-muted-foreground">Default: {defaultText}</span>
      </div>
    </div>
  );
}

function displayFieldValue(field: ConfigFieldView): string {
  return formatConfigValue(field.current_value);
}

export function ConfigFilterBar({
  filter,
  shownCount,
  onSelectFilter,
}: {
  filter: string;
  shownCount: number;
  onSelectFilter: (id: string) => void;
}) {
  return (
    <div className={cn("sticky top-0 z-10 flex-wrap items-center gap-1.5 border-b border-border bg-background py-2", FLEX)}>
      <span className="mr-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Filter
      </span>
      {CONFIG_FILTERS.map((f) => (
        <button
          key={f.id}
          type="button"
          onClick={() => onSelectFilter(f.id)}
          aria-pressed={filter === f.id}
          data-testid={`filter-${f.id}`}
          className={`h-6 rounded border px-2 text-xs transition-colors ${
            filter === f.id
              ? "border-primary bg-primary font-medium text-primary-foreground"
              : "border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground"
          }`}
        >
          {f.label}
        </button>
      ))}
      {filter !== "all" && (
        <span className="ml-auto text-xs text-muted-foreground">
          {shownCount} setting{shownCount === 1 ? "" : "s"}
        </span>
      )}
    </div>
  );
}
