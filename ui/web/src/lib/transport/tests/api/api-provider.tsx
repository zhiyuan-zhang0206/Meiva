import type { ReactNode } from "react";

import { createApi, type Api } from "../../gateway/api-factory";
import { createHttp } from "../../gateway/http";
import type { TestGateway } from "../../gateway/tests/gateway-server";
import { httpRig, type HttpRig } from "../../gateway/tests/http-rig";
import { realNetwork } from "../../gateway/tests/real-network";
import { systemHttpTransport } from "../../system-transport";
import { ApiProvider } from "../../transport-context";

export function apiWrapper(api: Api): ({ children }: { children: ReactNode }) => ReactNode {
  return function ApiWrapper({ children }) {
    return <ApiProvider api={api}>{children}</ApiProvider>;
  };
}

// meiva:note
// Presentation tests seed their complete QueryClient state and perform no HTTP work.
// Keep the real API over the setup file's network guard: an accidental cold read fails
// at that existing guard instead of receiving a fabricated business result.
export function cacheOnlyApi(): Api {
  return createApi(createHttp(systemHttpTransport(), "", { reportSlow: () => undefined }));
}

// meiva:note
// The public test door shares the existing gateway fixtures without exposing
// the production gateway implementation to hook and component modules.
export { startGateway, type GatewayReply, type TestGateway } from "../../gateway/tests/gateway-server";
export {
  httpRig,
  keysWithoutRandomUUID,
  noUpload,
  sequentialKeys,
  type HttpRig,
} from "../../gateway/tests/http-rig";
export { realNetwork } from "../../gateway/tests/real-network";
export { UncancellableFetch } from "./uncancellable-fetch";

// meiva:note
// Consumer tests compose the existing rig with the captured platform network.
// The pure rig still accepts explicit resources; this public test door owns
// platform selection and observes each request's actual cancellation signal.
export function gatewayRig(gateway: TestGateway): HttpRig {
  const platform = systemHttpTransport();
  return httpRig({
    base: gateway.origin,
    fetch: realNetwork.fetch,
    now: () => platform.now(),
    keys: platform.keys,
    openUpload: () => new realNetwork.XMLHttpRequest(),
  });
}
