"use client";

import { useEffect } from "react";
import { useBrowser } from "@/lib/browser/browser-provider";
import { observeViewportAnomalies } from "@/lib/telemetry/viewport-anomaly";

export function ViewportAnomalyDiagnostic() {
  const browser = useBrowser();
  useEffect(() => observeViewportAnomalies(document, browser), [browser]);
  return null;
}
