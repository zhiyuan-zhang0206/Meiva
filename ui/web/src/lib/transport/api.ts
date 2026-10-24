import { API_BASE, trackTransport } from "./api-base";
import { createApi } from "./gateway/api-factory";
import { createHttp } from "./gateway/http";
import { systemHttpTransport } from "./system-transport";

export { ApiError } from "./gateway/plans";
export { MessageDeliveryUnknownError } from "./gateway/plans";
export { API_BASE } from "./api-base";
export type { Api } from "./gateway/api-factory";

export const assetUrl = (path: string): string => `${API_BASE}${path}`;

// meiva:note
// The one gateway client of the running app: the real fetch, clock and XHR
// against the configured gateway origin, with slow requests reported to the
// transport telemetry sink. Hooks and components receive it through ApiProvider
// (transport-context.tsx); a few call sites still import it directly.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
export const api = createApi(
  createHttp(systemHttpTransport(), API_BASE, {
    reportSlow: (report) => {
      trackTransport("api-timing", report);
    },
  }),
);
