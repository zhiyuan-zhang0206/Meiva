"use client";

import * as Popover from "@radix-ui/react-popover";
import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { SpawnForm } from "@/components/agents/spawn-form";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { cn } from "@/lib/format/utils";

import { SidebarIconButton } from "./icon-button";

// meiva:note
// The spawn controls - machine, model, thinking effort, harness - are a form,
// and the sidebar is a list. They sat in a permanent row that a 230px column
// could not fit: every picker truncated to an unreadable fragment while the
// text button took 38% of the width (user 2026-10-20). They now live in a
// popover behind the header's +, and the form itself is untouched: this shell
// only decides when SpawnButton is mounted.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

interface Props {
  onSpawn: Parameters<typeof SpawnForm>[0]["onSpawn"];
  variant: "icon" | "cta";
}

export function SpawnPopover({ onSpawn, variant }: Props) {
  const t = useTranslations("spawn");
  const { isNarrow } = useBreakpoint();
  const [open, setOpen] = useState(false);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        {variant === "icon" ? (
          <SidebarIconButton label={t("newAgent")} testid="sidebar-create">
            <Plus className="size-4" />
          </SidebarIconButton>
        ) : (
          <button
            type="button"
            data-testid="sidebar-create-cta"
            className="w-full inline-flex items-center justify-center gap-1.5 rounded-md bg-primary px-2 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="size-4" />
            <span>{t("newAgent")}</span>
          </button>
        )}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          align="end"
          className={cn("z-50 w-[660px] max-w-[94vw] text-popover-foreground outline-none")}
        >
          <SpawnForm
            onSpawn={onSpawn}
            onDone={() => setOpen(false)}
            layout={isNarrow ? "stack" : "table"}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
