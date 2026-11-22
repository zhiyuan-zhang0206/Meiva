// Layout controls for the Fleet relationship graph.

"use client";

import * as Popover from "@radix-ui/react-popover";
import { Settings2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, useCallback, useMemo } from "react";

import { useDebouncedSetting } from "@/lib/state/use-user-settings";
import { FLEX, FLEX_COL } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

// ── Tunable d3-force layout parameters ──
// The physics knobs the user can adjust live (DB-backed via useForceParams).
// centerForceX / centerForceY are a forceX/forceY pull toward the origin
// (gravity), disabled when the strength is 0.
export interface ForceParams {
  // Node
  nodeSizeMin: number; // minimum node circle radius (pixels)
  nodeSizeMax: number; // maximum node circle radius (pixels)
  // Edge
  linkDistance: number; // edge ideal length
  linkStrength: number; // edge spring strength
  // Layout
  repulsion: number; // forceManyBody charge magnitude (applied as -repulsion)
  centerStrength: number; // forceCenter pull strength (1 = default; higher = tighter clusters)
  centerForceX: number; // forceX strength (0 = disabled; 0.1 = default d3 strength)
  centerForceY: number; // forceY strength (0 = disabled; 0.1 = default d3 strength)
  collidePadding: number; // extra spacing added to each node radius in forceCollide
  alphaDecay: number; // simulation cooling rate (d3 default ~0.0228; lower = longer to settle)
  // Zoom
  zoomPadding: number; // extra viewport padding when focusing a node + its neighbors
  zoomFitRatio: number; // viewport fill ratio when focusing (1.0 = fill viewport; 0.5 = half)
}

export function useForceParams(
): {
  params: ForceParams;
  setParams: (p: ForceParams) => void;
  reset: () => void;
} {
  const [stored, setStored] = useDebouncedSetting<Partial<ForceParams> | undefined>(
    "display.graph_force_params",
    undefined,
  );
  const params = useMemo<ForceParams>(() => ({ ...FORCE_DEFAULTS, ...(stored ?? {}) }), [stored]);
  const setParams = useCallback((p: ForceParams) => setStored(p), [setStored]);
  const reset = useCallback(() => setStored(FORCE_DEFAULTS), [setStored]);
  return { params, setParams, reset };
}

// Layout-tuning popover grouped by category. Each slider maps to a ForceParams
// key; changes apply live (the running simulation is re-heated) and persist to
// the DB through a debounced write.
type ForceLabelKey =
  | "node"
  | "edge"
  | "layout"
  | "zoom"
  | "minSize"
  | "maxSize"
  | "distance"
  | "strength"
  | "repulsion"
  | "centerPull"
  | "nodeSpacing"
  | "xGravity"
  | "yGravity"
  | "coolSpeed"
  | "fillRatio"
  | "padding";

interface ForceSliderDef {
  key: keyof ForceParams;
  label: ForceLabelKey;
  min: number;
  max: number;
  step: number;
}
interface ForceGroup {
  label: ForceLabelKey;
  sliders: ForceSliderDef[];
}

// meiva:note
// alphaDecay defaults to 0.08 rather than d3's 0.0228. At 0.0228 the 40-node
// Agent Graph ran 295 ticks — 4.9 s at 60 fps — and every node travelled 108 px
// on average before it stopped, which the user read as "too much movement"
// while still liking the landing itself (2026-10-21). 0.08 settles in 82 ticks
// (1.4 s) with the same layout and no overlapping nodes, and the entrance still
// shows a landing. Only a default changes: a browser that already stored
// display.graph_force_params keeps its value, and the slider range stays 0.001-0.2.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
const FORCE_DEFAULTS: ForceParams = {
  nodeSizeMin: 18,
  nodeSizeMax: 26,
  linkDistance: 70,
  linkStrength: 0.44,
  repulsion: 360,
  centerStrength: 0.9,
  centerForceX: 0.2,
  centerForceY: 0.2,
  collidePadding: 6,
  alphaDecay: 0.08,
  zoomPadding: 24,
  zoomFitRatio: 1,
};

const FORCE_GROUPS: ForceGroup[] = [
  {
    label: "node",
    sliders: [
      { key: "nodeSizeMin", label: "minSize", min: 4, max: 30, step: 1 },
      { key: "nodeSizeMax", label: "maxSize", min: 8, max: 60, step: 1 },
    ],
  },
  {
    label: "edge",
    sliders: [
      { key: "linkDistance", label: "distance", min: 20, max: 400, step: 5 },
      { key: "linkStrength", label: "strength", min: 0, max: 1, step: 0.02 },
    ],
  },
  {
    label: "layout",
    sliders: [
      { key: "repulsion", label: "repulsion", min: 0, max: 10000, step: 20 },
      { key: "centerStrength", label: "centerPull", min: 0, max: 5, step: 0.1 },
      { key: "collidePadding", label: "nodeSpacing", min: 0, max: 40, step: 1 },
      { key: "centerForceX", label: "xGravity", min: 0, max: 1, step: 0.02 },
      { key: "centerForceY", label: "yGravity", min: 0, max: 1, step: 0.02 },
      { key: "alphaDecay", label: "coolSpeed", min: 0.001, max: 0.2, step: 0.001 },
    ],
  },
  {
    label: "zoom",
    sliders: [
      { key: "zoomFitRatio", label: "fillRatio", min: 0.1, max: 1, step: 0.05 },
      { key: "zoomPadding", label: "padding", min: 0, max: 120, step: 4 },
    ],
  },
];

export const ForceControls = memo(function ForceControls({
  params,
  setParams,
  reset,
}: {
  params: ForceParams;
  setParams: (p: ForceParams) => void;
  reset: () => void;
}) {
  const t = useTranslations("fleet.force");
  return (
    <Popover.Root>
      <Popover.Trigger
        className={cn("items-center justify-center rounded border border-border bg-background/80 p-1 text-muted-foreground backdrop-blur hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring", FLEX)}
        aria-label={t("settings")}
      >
        <Settings2 className="size-3.5" aria-hidden />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          align="start"
          className="z-50 w-64 rounded-md border border-border bg-popover p-3 text-popover-foreground shadow-md outline-none"
        >
          <div className={cn("mb-2 items-center justify-between", FLEX)}>
            <span className="text-xs font-medium">{t("layout")}</span>
            <button
              type="button"
              onClick={reset}
              className="text-[10px] text-muted-foreground underline decoration-dotted hover:text-foreground"
            >
              {t("resetAll")}
            </button>
          </div>
          <div className={cn("gap-3", FLEX, FLEX_COL)}>
            {FORCE_GROUPS.map((group) => (
              <div key={group.label}>
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
                  {t(group.label)}
                </div>
                <div className={cn("gap-2", FLEX, FLEX_COL)}>
                  {group.sliders.map((s) => (
                    <label key={s.key} className={cn("gap-0.5", FLEX, FLEX_COL)}>
                      <span className={cn("items-center justify-between text-[10px] text-muted-foreground", FLEX)}>
                        <span>{t(s.label)}</span>
                        <span className="tabular-nums text-foreground">
                          {params[s.key] < 0.01 && params[s.key] > 0
                            ? params[s.key].toFixed(4)
                            : String(params[s.key])}
                        </span>
                      </span>
                      <input
                        type="range"
                        min={s.min}
                        max={s.max}
                        step={s.step}
                        value={params[s.key]}
                        onChange={(e) => setParams({ ...params, [s.key]: Number(e.target.value) })}
                        className="w-full accent-sky-500"
                        aria-label={t("slider", { group: t(group.label), label: t(s.label) })}
                      />
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
});
