"use client";

// meiva:note
// The per-category switches of the notification spine (a question was asked, an agent has unread
// output), rendered from the gateway's catalog. A category the user has not touched shows the
// server's default; toggling one writes the whole switch map to
// `notifications.categories`, which every transport already honors.
// meiva:link base/events/notifications/README.md

import { Bell } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { readStoredSetting, useUserSettings } from "@/lib/state/use-user-settings";
import { ToggleRow } from "./_rows";
import { useApi } from "@/lib/transport/transport-context";

const SWITCHES_KEY = "notifications.categories";

// meiva:note
// Localized labels for the core categories; an id the table does not know shows the label the
// catalog carries (the catalog is data, not a build step).
const KNOWN_LABELS = {
  "core.question": "coreQuestion",
  "core.unread": "coreUnread",
} as const;

type KnownCategoryLabel = (typeof KNOWN_LABELS)[keyof typeof KNOWN_LABELS];

// meiva:note
// The lookup answers the label key for an id the table knows, without assuming every id is one of its keys.
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
