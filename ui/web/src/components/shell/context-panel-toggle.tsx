"use client";

import { PanelRight } from "lucide-react";
import { useTranslations } from "next-intl";

const pressedClass = "p-1 rounded shrink-0 hover:bg-sidebar-accent";

// meiva:note
// The right sidebar (Shared context) collapses completely like the left one; its title-bar button is PanelRight, the mirror of the left PanelLeft.
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
export function ContextPanelToggle({ open, onToggle, iconClass = "size-5" }: { open: boolean; onToggle: () => void; iconClass?: string }) {
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
      <PanelRight className={iconClass} />
    </button>
  );
}
