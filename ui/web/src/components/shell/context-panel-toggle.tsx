"use client";

import { FileText } from "lucide-react";
import { useTranslations } from "next-intl";

const pressedClass = "p-1 rounded shrink-0 hover:bg-sidebar-accent";

// meiva:note
// The right region is a panel of the main area, not a sidebar: only the agent tree
// is chrome. Its button is the document glyph, because that is what the panel
// shows (the active agent's to-user document + inspector), not a collapsing rail.
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
      <FileText className={iconClass} />
    </button>
  );
}
