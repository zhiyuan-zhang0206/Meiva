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
  transport.getPlugins.mockResolvedValue({plugins:[{name:"test_plugin",enabled:true,user_toggleable:true,category:"extension",description:"Useful capability"}]});
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
  transport.getPlugins.mockResolvedValue({plugins:[{name:"test_plugin",enabled:true,user_toggleable:true,category:"extension",description:""}]});
  transport.putPluginEnabled.mockRejectedValue(new Error("Policy unavailable"));
  renderPlugins();
  fireEvent.click(await screen.findByRole("switch",{name:"Toggle test_plugin"}));
  expect(await screen.findByText(/Policy unavailable/)).toBeTruthy();
  expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  expect(screen.queryByRole("status")).toBeNull();
});


it("groups declared LLM and Decision providers without switches, regardless of ID", async () => {
  transport.getPlugins.mockResolvedValue({ plugins: [
    { name: "anthropic_adapter", display_name: "Anthropic", enabled: true, description: "", user_toggleable: false, category: "provider", provider_kinds: ["llm"] },
    { name: "lm_typesafe", display_name: "TypeSafe", enabled: true, description: "", user_toggleable: false, category: "provider", provider_kinds: ["decision"] },
    { name: "lm_regular", enabled: true, description: "Functional plugin", provider_kinds: [], user_toggleable: true, category: "builtin" },
    { name: "protected_tool", enabled: true, description: "Read-only plugin", category: "extension", provider_kinds: [], user_toggleable: false },
    { name: "dual", display_name: "Dual", enabled: true, description: "", provider_kinds: ["llm", "decision"], user_toggleable: false, category: "provider" },
  ] });
  renderPlugins();
  expect(await screen.findByText("LLM · Anthropic")).toBeTruthy();
  expect(screen.getByText("Decision · TypeSafe")).toBeTruthy();
  expect(screen.getByText("LLM · Dual")).toBeTruthy();
  expect(screen.getByText("Decision · Dual")).toBeTruthy();
  expect(screen.getByRole("region", { name: "LLM providers" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "Decision model providers" })).toBeTruthy();
  expect(screen.getAllByRole("switch")).toHaveLength(1);
  expect(screen.getByRole("switch", { name: "Toggle lm_regular" })).toBeTruthy();
  expect(screen.getByText("protected_tool")).toBeTruthy();
  expect(screen.getByRole("region", { name: "Built-in plugins" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "Extensions" })).toBeTruthy();
  expect(transport.putPluginEnabled).not.toHaveBeenCalled();
});
