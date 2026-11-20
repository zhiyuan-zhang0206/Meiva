"use client";

import { PanelRight } from "lucide-react";
import { useTranslations } from "next-intl";

import { ICON_SIZE_CLASS } from "@/lib/layout/layout";

const pressedClass = "p-1 rounded shrink-0 hover:bg-sidebar-accent";

// meiva:note
// The collapsed right panel reopens from the same right-panel glyph its own header
// uses to close it, mirroring the left sidebar's PanelLeft (user ruling 2026-11-20).
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.md
export function ContextPanelToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const t = useTranslations("sidebar");
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={t(open ? "hideContextPanel" : "showContextPanel")}
      aria-pressed={open}
      data-testid="context-panel-toggle"
      className={`${pressedClass} ${open ? "text-foreground" : "text-muted-foreground"}`}
    >
      <PanelRight className={ICON_SIZE_CLASS} />
    </button>
  );
}
