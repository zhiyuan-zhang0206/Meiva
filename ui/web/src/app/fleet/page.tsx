// /fleet — full-screen multi-agent supervision view. Renders the live agent
// relationship graph (spawn/fork/resurrect lineage + decaying message traffic)
// with each agent's self-reported activity, so a human can judge many agents at
// once without opening any single conversation. See components/fleet/fleet-view.tsx.
//
// FleetView reads the shared ["agents", "live"] cache (useFleetAgents), kept live by
// The R4 fold owner (inside EventStreamProvider) — the single writer subscribed to the global /api/system
// broadcast provided once at the app root (components/providers.tsx) — so it
// stays fresh across page navigation without the fleet view running its own SSE
// merge.

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useCallback, type ComponentProps, type ComponentType } from "react";

import { ErrorBoundary } from "@/components/connection/error-boundary";
import { FleetView } from "@/components/fleet/fleet-view";
import { FLEX, FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export interface FleetPageViewProps {
  FleetView: ComponentType<ComponentProps<typeof FleetView>>;
  navigate: (href: Route) => void;
}

// meiva:note
// This route is the fleet page's composition root: it reads the router, hands the surface a
// `navigate`, and names the surface itself for the view, which owns the landmark and the boundary.
// A test renders the view with its own stand-in surface and a spy instead of replacing either
// module.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export default function FleetPage() {
  const router = useRouter();
  const navigate = useCallback((href: Route) => { router.push(href); }, [router]);
  return <FleetPageView FleetView={FleetView} navigate={navigate} />;
}

export function FleetPageView({ FleetView, navigate }: FleetPageViewProps) {
  // meiva:note
  // <main> landmark (Task #1051 a11y) -- the fleet page is a primary surface; same pattern as
  // app/page.tsx.
  // meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, MIN_W_0)}>
      <ErrorBoundary>
        <FleetView navigate={navigate} />
      </ErrorBoundary>
    </main>
  );
}
