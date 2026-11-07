import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { renderWithApi } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
import { fakeNavigation, navigationWrapper } from "@/lib/browser/navigation/testing";
import { PluginsInventory } from "./page";

afterEach(cleanup);
beforeEach(resetTransport);

function renderPlugins() {
  const Page = navigationWrapper(fakeNavigation());
  return renderWithApi(<Page><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})}><PluginsInventory /></QueryClientProvider></Page>);
}

it("shows one cluster switch and saves once without machine writes", async () => {
  transport.getPlugins.mockResolvedValue({plugins:[{name:"test_plugin",enabled:true,description:"Useful capability"}]});
  transport.putPluginEnabled.mockResolvedValue({name:"test_plugin",enabled:false,restart_required:true});
  renderPlugins();
  fireEvent.click(await screen.findByRole("switch",{name:"Toggle test_plugin"}));
  await waitFor(()=>expect(transport.putPluginEnabled).toHaveBeenCalledWith("test_plugin",false));
  expect(transport.putPluginEnabled).toHaveBeenCalledTimes(1);
  expect(transport.getInventory.mock.calls).toHaveLength(0);
  expect(transport.putInventory.mock.calls).toHaveLength(0);
  expect(await screen.findByRole("status")).toBeTruthy();
});

it("keeps the saved switch value and reports a rejected write", async () => {
  transport.getPlugins.mockResolvedValue({plugins:[{name:"test_plugin",enabled:true,description:""}]});
  transport.putPluginEnabled.mockRejectedValue(new Error("Policy unavailable"));
  renderPlugins();
  fireEvent.click(await screen.findByRole("switch",{name:"Toggle test_plugin"}));
  expect(await screen.findByText(/Policy unavailable/)).toBeTruthy();
  expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  expect(screen.queryByRole("status")).toBeNull();
});
