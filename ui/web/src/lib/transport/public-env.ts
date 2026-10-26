import type { GatewayOriginOptions } from "./gateway-origin";

// meiva:note
// The build-time public configuration of the console. Next inlines `process.env.NEXT_PUBLIC_*` only
// where the literal expression stands in the source, so these reads cannot be handed in from the
// caller; this module is the one place they are written, and everything else takes the value.
export const publicEnv: GatewayOriginOptions = {
  apiBase: process.env.NEXT_PUBLIC_API_BASE,
  browserOrigin: process.env.NEXT_PUBLIC_BROWSER_ORIGIN,
  gatewayPort: process.env.NEXT_PUBLIC_GATEWAY_PORT,
};
