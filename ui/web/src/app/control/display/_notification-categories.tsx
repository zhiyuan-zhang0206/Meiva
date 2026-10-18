"use client";

// meiva:note
// The per-category switches of the notification spine, rendered from the
// gateway's merged catalog. A category the user has not touched shows the
// server's default; toggling one writes the whole switch map to
// `notifications.categories`, which every transport already honors.
// meiva:link base/events/notifications/docs/notifications.meiva.okf.md

import { Bell } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { api } from "@/lib/transport/api";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { ToggleRow } from "./_rows";

const SWITCHES_KEY = "notifications.categories";

// meiva:note
// Localized labels for the categories the core owns; a plugin category falls back
// to the label its declaration carries (the catalog is data, not a build step).
const KNOWN_LABELS = {
  "core.turn_finished": "coreTurnFinished",
  "core.needs_input": "coreNeedsInput",
  "meiva_fleet.task_closed": "fleetTaskClosed",
} as const;

export function NotificationCategoryRows() {
  const t = useTranslations("displaySettings.categories");
  const { settings, setSetting } = useUserSettings();
  const { data } = useQuery({
    queryKey: ["notification-categories"],
    queryFn: () => api.getNotificationCategories(),
    staleTime: 60_000,
  });

  const switches = (settings[SWITCHES_KEY] ?? {}) as Record<string, boolean>;
  const categories = data?.categories ?? [];
  if (categories.length === 0) return null;

  const setCategory = (id: string, enabled: boolean) => {
    setSetting(SWITCHES_KEY, { ...switches, [id]: enabled });
  };

  return (
    <>
      {categories.map((category) => (
        <ToggleRow
          key={category.id}
          icon={Bell}
          label={
            category.id in KNOWN_LABELS
              ? t(KNOWN_LABELS[category.id as keyof typeof KNOWN_LABELS])
              : category.label
          }
          description={category.description}
          value={switches[category.id] ?? category.enabled}
          onChange={(enabled) => setCategory(category.id, enabled)}
        />
      ))}
    </>
  );
}
