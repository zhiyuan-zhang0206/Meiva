"use client";

// /control#skills — this gateway host's installed skills.
//
// Read: GET /api/skills returns a SkillsView — every top-level directory in the
// host's `$MEIVA_HOME/skills/` load dir correlated with the install registry:
// name, source layer (core=repo / plugin / machine=user / untracked), and the
// `enabled` toggle the skill scanner honors. Per-machine (skills aren't a
// cross-machine matrix like plugins/MCP).
//
// Adding one is not a form: PackageDraftEntry hands a natural-language request
// to an installer agent (POST /api/keyed/v1/packages/draft) that finds, installs, and
// verifies the skill in its own conversation.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { PackageDraftEntry } from "@/components/plugins/package-draft-entry";
import { Switch } from "@/components/ui/switch";
import { InlineError } from "@/components/ui/inline-error";

import { errMsg } from "@/lib/contracts/errors";
import { useStore } from "@/lib/state/store";
import type { SkillLayer, SkillsView } from "@/lib/contracts/types";

import { useSectionVisible } from "../visibility";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { useApi } from "@/lib/transport/transport-context";

const SKILLS_QUERY_KEY = ["skills"] as const;

function layerLabel(layer: SkillLayer, t: ReturnType<typeof useTranslations<"skills">>): string {
  // The key mapping is literal — TS narrows each branch so the key is a
  // checked member of the skills namespace.
  switch (layer) {
    case "core":
      return t("layerCore");
    case "plugin":
      return t("layerPlugin");
    case "machine":
      return t("layerMachine");
    case "untracked":
      return t("layerUntracked");
  }
}

export default function SkillsPage() {
  const client = useApi();
  const t = useTranslations("skills");
  const showToast = useStore((s) => s.showToast);
  const visible = useSectionVisible();
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: client.getSkills,
    enabled: visible,
  });

  const toggleMutation = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      client.putSkillEnabled(name, enabled),
    // Without an onError the switch silently snapped back (invalidate pulled
    // the server truth) and the user never knew the toggle failed.
    onError: (err: unknown) => showToast(`Failed to toggle skill: ${errMsg(err)}`),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY });
    },
  });

  // The install entry stays mounted through every state — you can ask for a new
  // skill while the list is still loading, or when the read failed.
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        {t("intro")}
      </p>
      <PackageDraftEntry kind="skill" />
      <SkillsTable
        data={data}
        isLoading={isLoading}
        error={error}
        onToggle={(name, enabled) => toggleMutation.mutate({ name, enabled })}
        busy={toggleMutation.isPending}
      />
    </div>
  );
}

function SkillsTable({
  data,
  isLoading,
  error,
  onToggle,
  busy,
}: {
  data: SkillsView | undefined;
  isLoading: boolean;
  error: unknown;
  onToggle: (name: string, enabled: boolean) => void;
  busy: boolean;
}) {
  const t = useTranslations("skills");
  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (error) {
    return (
      <InlineError>{t("couldntLoad")}</InlineError>
    );
  }
  if (!data) return <p className="text-sm text-muted-foreground">{t("noData")}</p>;
  if (data.skills.length === 0) {
    return <p className="text-xs text-muted-foreground">{t("noSkills")}</p>;
  }

  return (
    <ul className="divide-y divide-border rounded-xl border border-border">
      {data.skills.map((skill) => (
        <li key={skill.name} className={cn(FLEX, "items-center justify-between gap-5 px-5 py-4")}>
          <div className={cn(MIN_W_0, "space-y-1")}>
            <p className="break-words text-sm font-medium">{skill.name}</p>
            <p className="text-xs text-muted-foreground">{layerLabel(skill.layer, t)}</p>
          </div>
          <Switch
            tone="neutral"
            checked={skill.enabled}
            onCheckedChange={(checked) => onToggle(skill.name, checked)}
            disabled={skill.layer === "untracked" || busy}
            aria-label={t("toggle", { name: skill.name })}
          />
        </li>
      ))}
    </ul>
  );
}
