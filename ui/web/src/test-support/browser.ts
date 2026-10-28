// meiva:note
// The component tests' door to the browser port's fake: it lives in lib/browser/testing.ts, beside
// the port, because the hooks in lib/ drive it first and lib/ may not import this directory (the
// cycle test-support -> lib already exists). Re-exported here so a component test reads one import
// path for its doubles, and `local/no-test-only-import` refuses that import from a shipped module.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

export { fakeBrowser, type FakeActions, type FakeBrowser, type FakeBrowserOptions } from "@/lib/browser/testing";
