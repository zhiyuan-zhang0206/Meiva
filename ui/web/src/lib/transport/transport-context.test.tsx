// meiva:note
// The provider is the only way to the client: a reader rendered outside it must fail
// loudly. Quietly borrowing the app's own gateway is the failure this guards against --
// a page would answer from production data while the test believes it supplied a fake.
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Api } from "./gateway/api-factory";
import { cacheOnlyApi } from "./tests/api/api-provider";
import { ApiProvider, useApi } from "./transport-context";

function Probe({ onClient }: { onClient: (client: Api) => void }) {
  onClient(useApi());
  return null;
}

afterEach(cleanup);

describe("useApi", () => {
  it("hands the provided client to the reader", () => {
    const client = cacheOnlyApi();
    const seen: Api[] = [];
    render(
      <ApiProvider api={client}>
        <Probe onClient={(provided) => seen.push(provided)} />
      </ApiProvider>,
    );
    expect(seen).toEqual([client]);
  });

  it("throws without a provider instead of borrowing the app's client", () => {
    expect(() => render(<Probe onClient={() => undefined} />)).toThrow(
      "useApi must be used inside <ApiProvider>",
    );
  });
});
