"use client";

// /control/schedules — schedule management: list, status, logs, run history,
// create (natural-language via a writer agent, or a raw script), edit, and
// start/stop/restart/delete.
//
// Backed by /api/schedules (gateway/schedules/router.py). The gateway's
// ScheduleManager supervises one session per enabled schedule; this page is
// the management surface.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  Loader2,
  Plus,
  Sparkles,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { CodeHighlighterPreloader } from "@/components/content/code-highlighter-preloader";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/transport/api";
import { errMsg } from "@/lib/contracts/errors";
import { useStore } from "@/lib/state/store";
import type { ScheduleCreate } from "@/lib/contracts/types";

import { useSectionVisible } from "../visibility";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { ScheduleForm, ScheduleRow } from "./_schedule_components";

const SCHEDULES_QUERY_KEY = ["schedules"] as const;

const EMPTY_FORM: ScheduleCreate = {
  name: "",
  script: "",
  command: "python schedule.py",
  description: null,
  enabled: true,
};

export default function SchedulesPage() {
  const t = useTranslations("schedules");
  const queryClient = useQueryClient();
  const showToast = useStore((s) => s.showToast);
  const setActiveId = useStore((s) => s.setActiveId);
  const router = useRouter();
  const visible = useSectionVisible();

  const { data: schedules, isLoading, error } = useQuery({
    queryKey: SCHEDULES_QUERY_KEY,
    queryFn: api.listSchedules,
    refetchInterval: 15_000,
    enabled: visible,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: SCHEDULES_QUERY_KEY });
  };
  const onErr = (verb: string) => (err: unknown) => showToast(`${verb} failed: ${errMsg(err)}`);

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteSchedule(id),
    onSuccess: invalidate,
    onError: onErr("Delete"),
  });
  const startMutation = useMutation({
    mutationFn: (id: number) => api.startSchedule(id),
    onSuccess: invalidate,
    onError: onErr("Start"),
  });
  const stopMutation = useMutation({
    mutationFn: (id: number) => api.stopSchedule(id),
    onSuccess: invalidate,
    onError: onErr("Stop"),
  });
  const restartMutation = useMutation({
    mutationFn: (id: number) => api.restartSchedule(id),
    onSuccess: () => {
      invalidate();
      showToast(t("restarted"));
    },
    onError: onErr("Restart"),
  });

  // --- natural-language create: spawn a writer agent, open its conversation ---
  const [nl, setNl] = useState("");
  const draftMutation = useMutation({
    mutationFn: (text: string) => api.draftSchedule(text),
    retry: false,
    onSuccess: (res) => {
      setNl("");
      setActiveId(res.agent_id);
      showToast(t("writerCreated", { id: res.agent_id }));
      router.push("/");
    },
    onError: onErr("Draft"),
  });

  // --- raw create form ---
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<ScheduleCreate>(EMPTY_FORM);
  const createMutation = useMutation({
    mutationFn: (body: ScheduleCreate) => api.createSchedule(body),
    onSuccess: () => {
      invalidate();
      setShowForm(false);
      setForm(EMPTY_FORM);
      showToast("Schedule created");
    },
    onError: onErr("Create"),
  });

  const [expandedId, setExpandedId] = useState<number | null>(null);

  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  // stale-while-error (Task #1051): a failed 15s poll must not swap the
  // table the user is looking at for an error page — only a cold failure
  // (nothing loaded yet) shows the error state.
  if (error && !schedules) {
    return (
      <div className="p-8 text-center text-sm text-muted-foreground">
        {t("couldntLoad")}
      </div>
    );
  }

  const list = schedules ?? [];

  return (
    <div className="space-y-4">
      {/* Warms the syntax-highlighter chunk once idle, ahead of a script
          actually being expanded — see python-code.tsx. */}
      <CodeHighlighterPreloader />
      <div className={cn("items-center justify-between", FLEX)}>
        <div className={cn("items-center gap-2", FLEX)}>
          <CalendarClock className="size-4" />
          <h3 className="text-sm font-semibold">{t("pageTitle")}</h3>
          <span className="text-xs text-muted-foreground">
            ({list.length} schedule{list.length !== 1 ? "s" : ""})
          </span>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => setShowForm((v) => !v)}>
          <Plus className="size-3.5 mr-1" />
          {t("newRaw")}
        </Button>
      </div>

      {/* --- Natural-language create --- */}
      <div className={cn("items-center gap-2", FLEX)}>
        <Input
          className="h-8 text-xs"
          placeholder="Describe a scheduled task — e.g. “consolidate memory nightly, or when it grows a lot”"
          value={nl}
          onChange={(e) => setNl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && nl.trim() && !draftMutation.isPending) draftMutation.mutate(nl.trim());
          }}
        />
        <Button
          type="button"
          size="sm"
          disabled={!nl.trim() || draftMutation.isPending}
          onClick={() => draftMutation.mutate(nl.trim())}
        >
          {draftMutation.isPending ? (
            <Loader2 className="size-3.5 animate-spin mr-1" />
          ) : (
            <Sparkles className="size-3.5 mr-1" />
          )}
          Describe
        </Button>
      </div>

      {/* --- Raw create form --- */}
      {showForm && (
        <ScheduleForm
          title={t("createTitle")}
          value={form}
          onChange={setForm}
          submitting={createMutation.isPending}
          submitLabel="Create"
          canSubmit={!!form.name && !!form.script}
          onSubmit={() => createMutation.mutate(form)}
          onCancel={() => setShowForm(false)}
        />
      )}

      {/* --- Table --- */}
      {list.length === 0 ? (
        <p className="text-xs text-muted-foreground py-4 text-center">{t("noSchedules")}</p>
      ) : (
        <div className="border border-border rounded-md overflow-x-auto">
          <Table className="[&_th]:border-r [&_th]:border-border [&_th:last-child]:border-r-0 [&_td]:border-r [&_td]:border-border [&_td:last-child]:border-r-0">
            <TableHeader>
              <TableRow>
                <TableHead className="w-6" />
                <TableHead className="w-16">{t("status")}</TableHead>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("description")}</TableHead>
                <TableHead>{t("updated")}</TableHead>
                <TableHead className="w-28" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.map((s) => (
                <ScheduleRow
                  key={s.id}
                  schedule={s}
                  expanded={expandedId === s.id}
                  onExpand={() => setExpandedId((id) => (id === s.id ? null : s.id))}
                  // Edit must ENSURE the row is expanded (toggle semantics
                  // collapsed an already-expanded row's details — Task #1051).
                  onEnsureExpanded={() => setExpandedId(s.id)}
                  onToggle={() =>
                    (s.enabled ? stopMutation : startMutation).mutate(s.id)
                  }
                  onRestart={() => restartMutation.mutate(s.id)}
                  onDelete={() => {
                    if (window.confirm(t("deleteConfirm", { name: s.name }))) deleteMutation.mutate(s.id);
                  }}
                  onEdited={invalidate}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
