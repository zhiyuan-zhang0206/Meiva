// Shared by browser requests and the server-rendered CSP. The configured
// HTTPS entry routes gateway paths directly; legacy direct frontend URLs
// still use the gateway's separate port, including during a staged rollout.
export interface GatewayOriginOptions {
  apiBase?: string;
  browserOrigin?: string;
  gatewayPort?: string;
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
  if (options.browserOrigin && requestUrl.origin === new URL(options.browserOrigin).origin) {
    return requestUrl.origin;
  }
  const gateway = new URL(requestUrl.origin);
  gateway.port = options.gatewayPort ?? '8000';
  return gateway.origin;
}
