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

import { readStoredSetting, useUserSettings } from "@/lib/state/use-user-settings";
import { ToggleRow } from "./_rows";
import { useApi } from "@/lib/transport/transport-context";

const SWITCHES_KEY = "notifications.categories";

// meiva:note
// Localized labels for the categories the core owns; a plugin category falls back
// to the label its declaration carries (the catalog is data, not a build step).
const KNOWN_LABELS = {
  "core.needs_input": "coreNeedsInput",
} as const;

type KnownCategoryLabel = (typeof KNOWN_LABELS)[keyof typeof KNOWN_LABELS];

// meiva:note
// The catalog is data, not a build step: a category the core owns has a localized label
// here, a plugin category carries its own. The lookup answers the label key for an id the
// table knows, without assuming every id is one of its keys.
function knownLabelFor(id: string): KnownCategoryLabel | undefined {
  const entry = Object.entries(KNOWN_LABELS).find(([knownId]) => knownId === id);
  return entry?.[1];
}

export function NotificationCategoryRows() {
  const client = useApi();
  const t = useTranslations("displaySettings.categories");
  const { settings, setSetting } = useUserSettings();
  const { data } = useQuery({
    queryKey: ["notification-categories"],
    queryFn: () => client.getNotificationCategories(),
    staleTime: 60_000,
  });

  const switches = readStoredSetting<Record<string, boolean>>(settings[SWITCHES_KEY], {});
  const categories = data?.categories ?? [];
  if (categories.length === 0) return null;

  const setCategory = (id: string, enabled: boolean) => {
    setSetting(SWITCHES_KEY, { ...switches, [id]: enabled });
  };

  return (
    <>
      {categories.map((category) => {
        const labelKey = knownLabelFor(category.id);
        return (
          <ToggleRow
            key={category.id}
            icon={Bell}
            label={labelKey ? t(labelKey) : category.label}
            description={category.description}
            value={switches[category.id] ?? category.enabled}
            onChange={(enabled) => setCategory(category.id, enabled)}
          />
        );
      })}
    </>
  );
}
