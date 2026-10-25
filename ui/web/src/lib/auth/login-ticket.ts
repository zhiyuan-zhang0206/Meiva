// One-time login link: `<ui>/#ticket=<ticket>`, printed by `meiva start`.
//
// The ticket rides in the URL fragment so it never reaches a server log or a
// Referer. It is removed from the address bar *before* it is sent anywhere, so
// it cannot linger in history or be copied from there; the gateway spends it on
// the first redemption and sets the ordinary session cookie.

import { API_BASE } from "../transport/api-base";

const TICKET_PARAM = "ticket";

// React StrictMode runs the mount effect twice in development; the second run
// must join the first redemption (the ticket is single-use) instead of finding
// an already-cleared fragment. Cleared once the redemption settles.
const ticketState: {
  inFlight: Promise<void> | undefined;
  fetchImpl: typeof fetch | undefined;
} = {
  inFlight: undefined,
  fetchImpl: undefined,
};

export interface LoginTicketLocation {
  readonly hash: string;
  readonly pathname: string;
  readonly search: string;
}

export interface LoginTicketRuntime {
  readonly location?: LoginTicketLocation;
  readonly replaceUrl?: (url: string) => void;
  readonly fetch?: typeof fetch;
}

export function takeTicketFromLocation(runtime?: LoginTicketRuntime): string | null {
  const loc = runtime?.location ?? window.location;
  const params = new URLSearchParams(loc.hash.replace(/^#/, ""));
  const ticket = params.get(TICKET_PARAM);
  if (!params.has(TICKET_PARAM)) return null;
  params.delete(TICKET_PARAM);
  const rest = params.toString();
  const nextUrl = `${loc.pathname}${loc.search}${rest ? `#${rest}` : ""}`;
  if (runtime?.replaceUrl) {
    runtime.replaceUrl(nextUrl);
  } else {
    window.history.replaceState(window.history.state, "", nextUrl);
  }
  return ticket;
}

// A ticket the gateway refuses (spent, expired, unknown, locked out) or never
// answers leaves the browser signed out; the session check that follows decides,
// and the cluster password still works. This module loads only for a URL that
// carries a ticket, so the request is made here rather than as a method of the
// `api` object every page ships.
async function redeem(
  ticket: string,
  fetchImpl: typeof fetch,
): Promise<void> {
  try {
    await fetchImpl(`${API_BASE}/api/auth/login-ticket/redeem`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticket }),
      credentials: "include",
    });
  } catch {
    // No verdict reached us; the session check reports the signed-out state.
  }
}

export function redeemTicketFromLocation(runtime?: LoginTicketRuntime): Promise<void> {
  if (ticketState.inFlight !== undefined) return ticketState.inFlight;
  const ticket = takeTicketFromLocation(runtime);
  if (!ticket) return Promise.resolve();
  const fetchImpl = runtime?.fetch ?? ticketState.fetchImpl ?? fetch;
  const pending = redeem(ticket, fetchImpl);
  ticketState.inFlight = pending;
  void pending.then(() => {
    if (ticketState.inFlight === pending) ticketState.inFlight = undefined;
  });
  return pending;
}

export function setTicketFetchForTest(fetchImpl?: typeof fetch): void {
  ticketState.fetchImpl = fetchImpl;
}
