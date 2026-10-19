"use client";

// meiva:note
// One section of the vertical Control / Insights pages: a stable anchor (`id` =
// URL hash + scroll target), an always-rendered heading (so the section stays
// Ctrl-F- and deep-link-reachable), the body, and a visibility provider that
// pauses only the section's polling once it scrolls out of view.
//
// The heading paints with the route; the body mounts one frame later (bounded by
// DEFERRED_MOUNT_DEADLINE_MS) because mounting nine bodies in the same
// transition is what held the URL back for seconds. Queries still start on the
// body's mount, long before a reader can scroll to it, so the page still lands
// populated. A deep-link jump keeps landing where it should: useSettledAnchorScroll
// re-applies it while content above the target is still growing.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md

import { type ReactNode } from "react";

import { useDeferredMount } from "@/lib/layout/use-client-ready";

import { SectionVisibilityProvider, useInView } from "./_visibility";

export function ControlSection({
  id,
  label,
  description,
  children,
}: {
  id: string;
  label: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  const [ref, inView] = useInView();
  const mounted = useDeferredMount();
  return (
    // scroll-mt keeps the heading off the very top edge when jumped to by anchor.
    <section id={id} ref={ref} className="scroll-mt-4">
      <header className="mb-3 border-b border-border pb-1">
        <h2 className="text-lg font-semibold text-foreground">{label}</h2>
        {description ? (
          <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        ) : null}
      </header>
      {mounted ? (
        <SectionVisibilityProvider visible={inView}>{children}</SectionVisibilityProvider>
      ) : null}
    </section>
  );
}
