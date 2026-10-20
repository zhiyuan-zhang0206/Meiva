"use client";

import * as React from "react";

import { cn } from "@/lib/format/utils";

// meiva:note
// One icon button for the whole rail: the header's row and the footer's nav
// shortcuts both render it, so padding, colour and hover cannot drift into two
// families again (user 2026-10-20: the header icons read as a different drawing
// than the row under them). Every button is the same quiet grey until hover,
// including the ones that create something: in a row of equal controls a
// brighter glyph reads as another family, and the empty roster carries its own
// full-width create button for discovery.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md

interface Props extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "className"> {
  label: string;
  title?: string;
  className?: string;
  testid?: string;
  ref?: React.Ref<HTMLButtonElement>;
  children: React.ReactNode;
}

export function SidebarIconButton({ label, title, className, testid, ref, children, ...rest }: Props) {
  return (
    <button
      ref={ref}
      type="button"
      aria-label={label}
      title={title}
      data-testid={testid}
      className={cn(
        "p-1.5 rounded shrink-0 transition-colors text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
