// meiva:note
// Shared by browser requests and the server-rendered CSP. Every configured HTTPS entry routes
// gateway paths directly on its own origin; legacy direct frontend URLs still use the gateway's
// separate port, including during a staged rollout. `browserOrigins` is the comma-separated entry
// list as configured (NEXT_PUBLIC_BROWSER_ORIGINS, the gateway's MEIVA_BROWSER_ORIGINS); unset or
// empty means no entry.
export interface GatewayOriginOptions {
  apiBase?: string;
  browserOrigins?: string;
  gatewayPort?: string;
}

// meiva:note
// The origins of a configured entry list. The gateway has already refused an invalid list at
// startup, so an entry that is not an absolute URL throws here too rather than being skipped.
function parseBrowserOrigins(configured: string | undefined): string[] {
  if (configured === undefined || configured.trim() === "") return [];
  return configured.split(",").map((entry) => new URL(entry.trim()).origin);
}

// meiva:note
// The base every gateway URL of a page is built on: the configured API base as it is written; none
// on the server (no page); in a browser, the origin the request rules pick for the page's address.
export function gatewayBaseForPage(options: GatewayOriginOptions, page: URL | null): string {
  if (options.apiBase) return options.apiBase;
  if (page === null) return "";
  return gatewayOriginForRequest(page, options);
}

export function gatewayOriginForRequest(requestUrl: URL, options: GatewayOriginOptions): string {
  if (options.apiBase) {
    const api = new URL(options.apiBase);
    if (!['http:', 'https:'].includes(api.protocol) || api.username || api.password) {
      throw new Error('Gateway URL must use http or https without credentials');
    }
    return api.origin;
  }
  if (parseBrowserOrigins(options.browserOrigins).includes(requestUrl.origin)) {
    return requestUrl.origin;
  }
  const gateway = new URL(requestUrl.origin);
  gateway.port = options.gatewayPort ?? '8000';
  return gateway.origin;
}
