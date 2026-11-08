"use client";

import { Smartphone } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_COL } from "@/lib/layout/layout";

import {
  type AppBridge,
  type AppUpdatePrompt,
  readAppBridge,
  useAppBridge,
} from "./bridge";

export { type AppBridge, type AppUpdatePrompt, readAppBridge, useAppBridge };

export function AppSettingsEntry({ bridge }: { bridge: AppBridge }) {
  const t = useTranslations("control");
  const [failed, setFailed] = useState(false);
  return (
    <div className={cn("items-start gap-2", FLEX, FLEX_COL)}>
      <Button
        size="sm"
        variant="secondary"
        onClick={() => {
          setFailed(false);
          void bridge.openSettings().catch(() => setFailed(true));
        }}
      >
        <Smartphone className="size-4" aria-hidden />
        {t("appOpenSettings")}
      </Button>
      {failed ? (
        <p role="status" className="text-xs text-destructive">
          {t("appOpenSettingsFailed")}
        </p>
      ) : null}
    </div>
  );
}

export function AppUpdateBanner() {
  const bridge = useAppBridge();
  const update = bridge?.update;
  if (!update) return null;
  return (
    <div
      role="region"
      aria-label="App update"
      data-meiva-app="update-banner"
      className={cn(
        "shrink-0 items-center justify-between gap-3 border-b border-border bg-muted/40 px-4 py-2 text-sm text-foreground",
        FLEX,
      )}
    >
      <span>{update.text}</span>
      <div className={cn("shrink-0 items-center gap-2", FLEX)}>
        <Button
          size="sm"
          disabled={update.status === "installing"}
          onClick={() => update.install()}
        >
          {update.actionLabel}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => update.dismiss()}>
          Later
        </Button>
      </div>
    </div>
  );
}
